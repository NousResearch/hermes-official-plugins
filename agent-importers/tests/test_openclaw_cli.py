"""OpenClaw migration commands (``hermes import-agent openclaw`` / ``hermes claw``), ported from core's
``tests/hermes_cli/test_claw.py``."""

import subprocess
import sys
from argparse import Namespace
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from agent_importers_plugin import openclaw as claw_mod


class TestBundledMigrationScript:
    def test_script_ships_next_to_the_skill_and_loads(self):
        assert claw_mod.MIGRATION_SCRIPT.parent.parent.joinpath("SKILL.md").is_file()
        mod = claw_mod._load_migration_module(claw_mod.MIGRATION_SCRIPT)
        assert callable(mod.Migrator)
        assert "secret-settings" not in mod.resolve_selected_options(None, None, preset="user-data")


# ---------------------------------------------------------------------------
# _find_openclaw_dirs
# ---------------------------------------------------------------------------


class TestFindOpenclawDirs:
    """Test discovery of OpenClaw directories."""

    def test_finds_openclaw_dir(self, tmp_path):
        openclaw = tmp_path / ".openclaw"
        openclaw.mkdir()
        with patch("pathlib.Path.home", return_value=tmp_path):
            found = claw_mod._find_openclaw_dirs()
        assert openclaw in found

    def test_finds_legacy_dirs(self, tmp_path):
        clawdbot = tmp_path / ".clawdbot"
        clawdbot.mkdir()
        moltbot = tmp_path / ".moltbot"
        moltbot.mkdir()
        with patch("pathlib.Path.home", return_value=tmp_path):
            found = claw_mod._find_openclaw_dirs()
        assert len(found) == 2
        assert clawdbot in found
        assert moltbot in found


# ---------------------------------------------------------------------------
# _scan_workspace_state
# ---------------------------------------------------------------------------


class TestScanWorkspaceState:
    """Test scanning for workspace state files."""

    def test_finds_root_state_files(self, tmp_path):
        (tmp_path / "todo.json").write_text("{}", encoding="utf-8")
        (tmp_path / "sessions").mkdir()
        findings = claw_mod._scan_workspace_state(tmp_path)
        descs = [desc for _, desc in findings]
        assert any("todo.json" in d for d in descs)
        assert any("sessions" in d for d in descs)

    def test_ignores_hidden_dirs(self, tmp_path):
        scan_dir = tmp_path / "scan_target"
        scan_dir.mkdir()
        hidden = scan_dir / ".git"
        hidden.mkdir()
        (hidden / "todo.json").write_text("{}", encoding="utf-8")
        findings = claw_mod._scan_workspace_state(scan_dir)
        assert len(findings) == 0


# ---------------------------------------------------------------------------
# _archive_directory
# ---------------------------------------------------------------------------


class TestArchiveDirectory:
    """Test directory archival (rename)."""

    def test_renames_to_pre_migration(self, tmp_path):
        source = tmp_path / ".openclaw"
        source.mkdir()
        (source / "test.txt").write_text("data", encoding="utf-8")

        archive_path = claw_mod._archive_directory(source)
        assert archive_path == tmp_path / ".openclaw.pre-migration"
        assert archive_path.is_dir()
        assert not source.exists()
        assert (archive_path / "test.txt").read_text(encoding="utf-8") == "data"

    def test_adds_timestamp_when_archive_exists(self, tmp_path):
        source = tmp_path / ".openclaw"
        source.mkdir()
        # Pre-existing archive
        (tmp_path / ".openclaw.pre-migration").mkdir()

        archive_path = claw_mod._archive_directory(source)
        assert ".pre-migration-" in archive_path.name
        assert archive_path.is_dir()
        assert not source.exists()

    def test_dry_run_does_not_rename(self, tmp_path):
        source = tmp_path / ".openclaw"
        source.mkdir()

        archive_path = claw_mod._archive_directory(source, dry_run=True)
        assert archive_path == tmp_path / ".openclaw.pre-migration"
        assert source.is_dir()  # Still exists


# ---------------------------------------------------------------------------
# migrate_command
# ---------------------------------------------------------------------------


def _fake_migration_module():
    fake_mod = ModuleType("openclaw_to_hermes")
    fake_mod.resolve_selected_options = MagicMock(return_value=set())
    fake_migrator = MagicMock()
    fake_migrator.migrate.return_value = {
        "summary": {"migrated": 0, "skipped": 0, "conflict": 0, "error": 0},
        "items": [],
    }
    fake_mod.Migrator = MagicMock(return_value=fake_migrator)
    return fake_mod


class TestMigrateCommand:
    """Test the migrate command handler."""

    @pytest.fixture(autouse=True)
    def _mock_openclaw_running(self):
        with patch.object(claw_mod, "_detect_openclaw_processes", return_value=[]):
            yield

    @pytest.mark.parametrize("migrate_secrets", [False, True])
    def test_full_preset_passes_migrate_secrets_through_unchanged(self, tmp_path, migrate_secrets):
        """The 'full' preset must NOT auto-enable migrate_secrets.

        Users have to opt in to secret import explicitly via --migrate-secrets, even under the
        'full' preset. This mirrors OpenClaw's migrate-hermes posture (two-phase import) and
        prevents a 'full' run from silently copying API keys.
        """
        openclaw_dir = tmp_path / ".openclaw"
        openclaw_dir.mkdir()
        fake_mod = _fake_migration_module()
        args = Namespace(
            source=str(openclaw_dir), dry_run=True, preset="full", overwrite=False,
            migrate_secrets=migrate_secrets, workspace_target=None, skill_conflict="skip",
            yes=False, no_backup=False)

        with patch.object(claw_mod, "_load_migration_module", return_value=fake_mod):
            claw_mod.migrate_command(args)

        assert fake_mod.Migrator.call_args[1]["migrate_secrets"] is migrate_secrets

    def test_unset_flags_take_the_claw_defaults(self, tmp_path):
        """``import-agent`` leaves OpenClaw-only flags at None so it can tell they were passed;
        the migration must still run with core's old defaults (full preset, skip conflicts)."""
        openclaw_dir = tmp_path / ".openclaw"
        openclaw_dir.mkdir()
        fake_mod = _fake_migration_module()
        args = Namespace(agent="openclaw", source=None, dry_run=True, overwrite=False, yes=False,
                         sync=False, preset=None, migrate_secrets=False, no_backup=False,
                         workspace_target=None, skill_conflict=None)

        with patch.object(claw_mod, "_load_migration_module", return_value=fake_mod):
            claw_mod.migrate_command(args)

        kwargs = fake_mod.Migrator.call_args[1]
        assert (kwargs["preset_name"], kwargs["skill_conflict_mode"]) == ("full", "skip")
        assert kwargs["source_root"] == openclaw_dir.resolve()


# ---------------------------------------------------------------------------
# cleanup_command
# ---------------------------------------------------------------------------


class TestCleanupCommand:
    """Test the cleanup command handler."""

    @pytest.fixture(autouse=True)
    def _mock_openclaw_running(self):
        with patch.object(claw_mod, "_detect_openclaw_processes", return_value=[]):
            yield

    def test_dry_run_lists_dirs(self, tmp_path, capsys):
        openclaw = tmp_path / ".openclaw"
        openclaw.mkdir()
        ws = openclaw / "workspace"
        ws.mkdir()
        (ws / "todo.json").write_text("{}", encoding="utf-8")

        args = Namespace(source=None, dry_run=True, yes=False)
        with patch.object(claw_mod, "_find_openclaw_dirs", return_value=[openclaw]):
            claw_mod.cleanup_command(args)

        captured = capsys.readouterr()
        assert "Would archive" in captured.out
        assert openclaw.is_dir()  # Not actually archived

    def test_explicit_source(self, tmp_path, capsys):
        custom_dir = tmp_path / "my-openclaw"
        custom_dir.mkdir()
        (custom_dir / "todo.json").write_text("{}", encoding="utf-8")

        args = Namespace(source=str(custom_dir), dry_run=False, yes=True)
        claw_mod.cleanup_command(args)

        captured = capsys.readouterr()
        assert "Archived" in captured.out
        assert not custom_dir.exists()


# ---------------------------------------------------------------------------
# _detect_openclaw_processes
# ---------------------------------------------------------------------------


class TestDetectOpenclawProcesses:

    @pytest.mark.skipif(sys.platform != "linux", reason="systemctl/pgrep probe sequence")
    def test_reports_union_of_exact_and_node_matches(self):
        with patch.object(claw_mod, "subprocess") as mock_subprocess:
            mock_subprocess.run.side_effect = [
                MagicMock(returncode=1, stdout=""),  # systemctl
                MagicMock(returncode=0, stdout="1234\n"),  # pgrep -x openclaw
                MagicMock(returncode=1, stdout=""),  # pgrep -x openclaw-gatewa
                MagicMock(returncode=1, stdout=""),  # pgrep -x clawd
                MagicMock(returncode=0, stdout="1234\n5678\n"),  # node cmdline probe
            ]
            mock_subprocess.TimeoutExpired = subprocess.TimeoutExpired
            result = claw_mod._detect_openclaw_processes()
        assert result == ["openclaw process(es) (PIDs: 1234, 5678)"]

    @pytest.mark.skipif(sys.platform != "linux", reason="needs /proc and Linux pgrep")
    def test_live_pgrep_ignores_argv_mentions_but_finds_node_openclaw(self, tmp_path):
        """A process that merely mentions "openclaw" in argv (the #12648 false positive) is not
        OpenClaw; a node interpreter running an openclaw script is."""
        import shutil
        import time
        from pathlib import Path

        # Nix's sys.executable can be a launcher that re-execs Python,
        # discarding both exec -a's argv[0] and the copied binary's comm.
        # Exercise actual process names with the running interpreter binary.
        executable = Path("/proc/self/exe").resolve()
        idle = f'{executable} -c "import time; time.sleep(30)"'
        # argv mentions openclaw but the binary is not one.
        bystander = subprocess.Popen(["bash", "-c", f"exec {idle} {tmp_path}/openclaw-notes.txt"])
        # argv[0] renamed to ``node`` running an openclaw script: the real launch shape.
        node_like = subprocess.Popen(["bash", "-c", f"exec -a node {idle} {tmp_path}/openclaw/entry.js"])
        # The gateway sets process.title="openclaw-gateway" (comm truncates to 15 chars); a
        # copied interpreter with that file name yields the same comm.
        titled_bin = tmp_path / "openclaw-gateway"
        shutil.copy2(executable, titled_bin)
        titled = subprocess.Popen([str(titled_bin), "-c", "import time; time.sleep(30)"])
        try:
            time.sleep(0.3)
            with patch.object(claw_mod, "_posix_probe", wraps=claw_mod._posix_probe) as probe:
                result = claw_mod._detect_openclaw_processes()
            assert not any(a[0][:2] == ["pgrep", "-f"] and a[0][2] == "openclaw" for a, _ in probe.call_args_list)
            assert len(result) == 1
            pids = result[0].split("PIDs: ")[1].rstrip(")").split(", ")
            assert str(node_like.pid) in pids
            assert str(titled.pid) in pids
            assert str(bystander.pid) not in pids
        finally:
            for proc in (bystander, node_like, titled):
                proc.kill()
                proc.wait()

    @pytest.mark.skipif(sys.platform != "win32", reason="tasklist/PowerShell branch")
    def test_returns_empty_on_windows_when_nothing_found(self):
        """Only a real Windows host resolves tasklist/powershell; nothing named openclaw runs on
        a CI runner, so the probe reports nothing."""
        assert claw_mod._detect_openclaw_processes() == []


class TestWarnIfOpenclawRunning:
    def test_noop_when_not_running(self, capsys):
        with patch.object(claw_mod, "_detect_openclaw_processes", return_value=[]):
            claw_mod._warn_if_openclaw_running(auto_yes=False)
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_warns_and_exits_when_running_and_user_declines(self, capsys):
        with patch.object(claw_mod, "_detect_openclaw_processes", return_value=["openclaw process(es) (PIDs: 1234)"]):
            with patch.object(claw_mod, "prompt_yes_no", return_value=False):
                with patch.object(claw_mod.sys.stdin, "isatty", return_value=True):
                    with pytest.raises(SystemExit) as exc_info:
                        claw_mod._warn_if_openclaw_running(auto_yes=False)
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "OpenClaw appears to be running" in captured.out
