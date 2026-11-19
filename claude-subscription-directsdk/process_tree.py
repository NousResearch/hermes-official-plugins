"""Own and kill a spawned Claude Code child and everything it started, as one process group."""
import os
import signal
import subprocess


def own_process_group():
    """Popen kwargs that put native (and the node/cmd children it spawns) in a group we can kill as one."""
    if os.name == 'nt':
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def unreaped(process, block=False):
    """Whether our leader is still unreaped (exited or not), observed without reaping it.

    While our unreaped leader holds the PID, its group id cannot be recycled, so signalling the
    group is safe even after a launcher exited and left children behind. Already reaped (by us or
    a host-wide ``waitpid(-1)`` reaper such as core's Kanban dispatcher): the PID is no longer
    proof of ownership, so the caller does nothing. ``os.waitid`` is missing on macOS before
    Python 3.13; there the old ``poll()`` guard stays.
    """
    if not hasattr(os, 'waitid'):
        return process.poll() is None
    try:
        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | (0 if block else os.WNOHANG))
    except ChildProcessError:
        return False
    return True


def kill_process_tree(process):
    """Kill native and every descendant: the npm shim is cmd.exe -> node on Windows, and a plain
    Popen.kill() would orphan the node child that holds the real request open."""
    if os.name == 'nt':
        if process.poll() is not None:
            return
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return
    if not unreaped(process):
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)  # windows-footgun: ok — the nt branch above never reaches this line
    except (ProcessLookupError, PermissionError):
        # ESRCH: already gone. EPERM: macOS answers killpg with EPERM once the group leader is a zombie.
        pass
