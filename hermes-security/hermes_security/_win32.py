"""Windows native primitives matching the POSIX containment the plugin relies on.

POSIX code pins paths with ``openat(dir_fd, name, O_NOFOLLOW)``. The NT
equivalent is ``NtCreateFile`` with ``OBJECT_ATTRIBUTES.RootDirectory`` (a
handle-relative open, so a swapped ancestor cannot redirect it) plus
``FILE_OPEN_REPARSE_POINT`` (open the link itself instead of following it).
Name-surrogate reparse points (symlinks, junctions, mount points) are refused
exactly like POSIX symlinks. Other reparse points (cloud placeholders, dedup)
do not redirect names, so they are reopened normally and identity-checked.

Process limits use Job Objects (the Windows analogue of a session plus
rlimits); ``0o700`` modes become protected owner-only DACLs.

Every ctypes signature is declared explicitly: on 64-bit (including ARM64)
an undeclared pointer argument is truncated to a C int.
"""
import ctypes
import errno
import msvcrt
import os
from ctypes import wintypes as w

ntdll = ctypes.WinDLL('ntdll')
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)

# --- NT file API -------------------------------------------------------------

class UNICODE_STRING(ctypes.Structure):
    _fields_ = [('Length', w.USHORT), ('MaximumLength', w.USHORT), ('Buffer', ctypes.c_void_p)]


class OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [('Length', w.ULONG), ('RootDirectory', w.HANDLE),
                ('ObjectName', ctypes.POINTER(UNICODE_STRING)), ('Attributes', w.ULONG),
                ('SecurityDescriptor', ctypes.c_void_p), ('SecurityQualityOfService', ctypes.c_void_p)]


class IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [('Status', ctypes.c_void_p), ('Information', ctypes.c_size_t)]


class FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = [('FileAttributes', w.DWORD), ('ReparseTag', w.DWORD)]


class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [('dwFileAttributes', w.DWORD), ('ftCreationTime', w.FILETIME),
                ('ftLastAccessTime', w.FILETIME), ('ftLastWriteTime', w.FILETIME),
                ('dwVolumeSerialNumber', w.DWORD), ('nFileSizeHigh', w.DWORD), ('nFileSizeLow', w.DWORD),
                ('nNumberOfLinks', w.DWORD), ('nFileIndexHigh', w.DWORD), ('nFileIndexLow', w.DWORD)]


NtCreateFile = ntdll.NtCreateFile
NtCreateFile.restype = ctypes.c_long
NtCreateFile.argtypes = [ctypes.POINTER(w.HANDLE), w.ULONG, ctypes.POINTER(OBJECT_ATTRIBUTES),
                         ctypes.POINTER(IO_STATUS_BLOCK), ctypes.c_void_p, w.ULONG, w.ULONG,
                         w.ULONG, w.ULONG, ctypes.c_void_p, w.ULONG]
NtSetInformationFile = ntdll.NtSetInformationFile
NtSetInformationFile.restype = ctypes.c_long
NtSetInformationFile.argtypes = [w.HANDLE, ctypes.POINTER(IO_STATUS_BLOCK), ctypes.c_void_p, w.ULONG, ctypes.c_int]
RtlNtStatusToDosError = ntdll.RtlNtStatusToDosError
RtlNtStatusToDosError.restype = w.ULONG
RtlNtStatusToDosError.argtypes = [ctypes.c_long]
NtResumeProcess = ntdll.NtResumeProcess
NtResumeProcess.restype = ctypes.c_long
NtResumeProcess.argtypes = [w.HANDLE]

CloseHandle = kernel32.CloseHandle
CloseHandle.restype = w.BOOL
CloseHandle.argtypes = [w.HANDLE]
GetFileInformationByHandleEx = kernel32.GetFileInformationByHandleEx
GetFileInformationByHandleEx.restype = w.BOOL
GetFileInformationByHandleEx.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
GetFileInformationByHandle = kernel32.GetFileInformationByHandle
GetFileInformationByHandle.restype = w.BOOL
GetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION)]

DELETE = 0x00010000
READ_CONTROL = 0x00020000
WRITE_DAC = 0x00040000
SYNCHRONIZE = 0x00100000
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_LIST_DIRECTORY = 0x0001
FILE_TRAVERSE = 0x0020
FILE_READ_ATTRIBUTES = 0x0080

FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
FILE_OPEN, FILE_CREATE = 1, 2
FILE_DIRECTORY_FILE = 0x00000001
FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
FILE_NON_DIRECTORY_FILE = 0x00000040
FILE_OPEN_REPARSE_POINT = 0x00200000
OBJ_CASE_INSENSITIVE = 0x40

FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
REPARSE_TAG_NAME_SURROGATE = 0x20000000

_FileAttributeTagInfo = 9
_FileRenameInformation, _FileRenameInformationEx = 10, 65
_FileDispositionInformation, _FileDispositionInformationEx = 13, 64

_DIR_ACCESS = FILE_TRAVERSE | FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | SYNCHRONIZE


class LinkRefused(OSError):
    """A path component is a symlink/junction (POSIX O_NOFOLLOW -> ELOOP)."""
    def __init__(self, name):
        super().__init__(errno.ELOOP, 'refusing to follow link', name)


def _nt_error(status, name=None):
    code = RtlNtStatusToDosError(status)
    return OSError(None, ctypes.FormatError(code).strip(), name, code)


def _check_component(name):
    if (not isinstance(name, str) or name in {'', '.', '..'}
            or any(c in name for c in '\\/:\x00')):
        raise OSError(errno.EINVAL, 'invalid path component', name)


class Handle:
    """Owned NT handle. Closing is idempotent; ``detach`` transfers ownership."""
    __slots__ = ('h',)

    def __init__(self, h):
        self.h = h

    def close(self):
        if self.h:
            CloseHandle(self.h)
            self.h = None

    def detach(self):
        h, self.h = self.h, None
        return h

    def into_fd(self, flags=0):
        """Transfer the handle to a CRT descriptor usable with os.read/os.fdopen."""
        fd = msvcrt.open_osfhandle(self.h, flags)
        self.h = None
        return fd

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        self.close()


def _open(root, name, access, disposition, options, *, allow_empty=False):
    if not (allow_empty and name == '') and root is not None:
        _check_component(name)
    buf = ctypes.create_unicode_buffer(name)
    size = ctypes.sizeof(buf) - ctypes.sizeof(ctypes.c_wchar)
    us = UNICODE_STRING(size, size + ctypes.sizeof(ctypes.c_wchar), ctypes.cast(buf, ctypes.c_void_p))
    oa = OBJECT_ATTRIBUTES(ctypes.sizeof(OBJECT_ATTRIBUTES), root.h if root is not None else None,
                           ctypes.pointer(us), OBJ_CASE_INSENSITIVE, None, None)
    h = w.HANDLE()
    iosb = IO_STATUS_BLOCK()
    status = NtCreateFile(ctypes.byref(h), access | SYNCHRONIZE, ctypes.byref(oa), ctypes.byref(iosb),
                          None, 0, FILE_SHARE_ALL, disposition, options | FILE_SYNCHRONOUS_IO_NONALERT, None, 0)
    if status < 0:
        raise _nt_error(status, name)
    return Handle(h.value)


def attribute_tag(handle):
    info = FILE_ATTRIBUTE_TAG_INFO()
    if not GetFileInformationByHandleEx(handle.h, _FileAttributeTagInfo, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return info.FileAttributes, info.ReparseTag


def file_identity(handle):
    info = BY_HANDLE_FILE_INFORMATION()
    if not GetFileInformationByHandle(handle.h, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return info.dwVolumeSerialNumber, (info.nFileIndexHigh << 32) | info.nFileIndexLow


def _open_nofollow(root, name, access, *, directory, disposition=FILE_OPEN):
    """Open ``name`` relative to ``root`` without following name-surrogate links."""
    kind = FILE_DIRECTORY_FILE if directory is True else FILE_NON_DIRECTORY_FILE if directory is False else 0
    h = _open(root, name, access | FILE_READ_ATTRIBUTES, disposition, kind | FILE_OPEN_REPARSE_POINT)
    try:
        attrs, tag = attribute_tag(h)
        if not attrs & FILE_ATTRIBUTE_REPARSE_POINT:
            return h
        if tag & REPARSE_TAG_NAME_SURROGATE:
            raise LinkRefused(name)
        # Non-redirecting reparse point (e.g. a cloud placeholder): let its filter
        # handle the data, but prove the reopen reached the same file.
        first = file_identity(h)
        again = _open(root, name, access | FILE_READ_ATTRIBUTES, FILE_OPEN, kind)
        if file_identity(again) != first:
            again.close()
            raise LinkRefused(name)
        h.close()
        return again
    except BaseException:
        h.close()
        raise


def nt_path(path):
    """Absolute Win32 path -> NT object path (``\\??\\C:\\x`` / ``\\??\\UNC\\s\\x``)."""
    text = os.fspath(path)
    if text.startswith('\\\\?\\'):
        return '\\??\\' + text[4:]
    if text.startswith('\\\\'):
        return '\\??\\UNC\\' + text[2:]
    return '\\??\\' + text


def open_volume_root(path):
    """Handle to the root directory of the volume containing absolute ``path``."""
    from pathlib import PureWindowsPath
    anchor = PureWindowsPath(path).anchor
    if not anchor:
        raise OSError(errno.EINVAL, 'path is not absolute', os.fspath(path))
    return _open(None, nt_path(anchor), _DIR_ACCESS, FILE_OPEN, FILE_DIRECTORY_FILE)


def open_path_nofollow(path):
    """Open an absolute path's final component itself (never a link target)."""
    h = _open(None, nt_path(path), READ_CONTROL | FILE_READ_ATTRIBUTES, FILE_OPEN, FILE_OPEN_REPARSE_POINT)
    try:
        attrs, tag = attribute_tag(h)
        if attrs & FILE_ATTRIBUTE_REPARSE_POINT and tag & REPARSE_TAG_NAME_SURROGATE:
            raise LinkRefused(os.fspath(path))
        return h
    except BaseException:
        h.close()
        raise


def open_dir_path(path):
    """Open an already-resolved absolute directory; refuse if it is itself a link."""
    h = _open(None, nt_path(path), _DIR_ACCESS, FILE_OPEN, FILE_DIRECTORY_FILE | FILE_OPEN_REPARSE_POINT)
    try:
        attrs, tag = attribute_tag(h)
        if attrs & FILE_ATTRIBUTE_REPARSE_POINT and tag & REPARSE_TAG_NAME_SURROGATE:
            raise LinkRefused(os.fspath(path))
        return h
    except BaseException:
        h.close()
        raise


def open_dir(parent, name):
    return _open_nofollow(parent, name, _DIR_ACCESS, directory=True)


def mkdir(parent, name):
    _check_component(name)
    _open(parent, name, FILE_LIST_DIRECTORY, FILE_CREATE, FILE_DIRECTORY_FILE).close()


def open_file_read(parent, name):
    return _open_nofollow(parent, name, GENERIC_READ, directory=False)


def create_file_exclusive(parent, name):
    """O_CREAT|O_EXCL|O_NOFOLLOW: fails if anything (including a link) exists."""
    return _open_nofollow(parent, name, GENERIC_WRITE | DELETE, directory=False, disposition=FILE_CREATE)


def lstat_kind(parent, name):
    """Return 'file', 'dir' or 'link' for ``name`` without following it."""
    h = _open(parent, name, FILE_READ_ATTRIBUTES, FILE_OPEN, FILE_OPEN_REPARSE_POINT)
    with h:
        attrs, tag = attribute_tag(h)
    if attrs & FILE_ATTRIBUTE_REPARSE_POINT and tag & REPARSE_TAG_NAME_SURROGATE:
        return 'link'
    return 'dir' if attrs & FILE_ATTRIBUTE_DIRECTORY else 'file'


def _rename_struct(name):
    class FILE_RENAME_INFORMATION(ctypes.Structure):
        _fields_ = [('Flags', w.ULONG), ('RootDirectory', w.HANDLE), ('FileNameLength', w.ULONG),
                    ('FileName', ctypes.c_wchar * (len(name) + 1))]
    return FILE_RENAME_INFORMATION


def rename_handle(raw_handle, parent, name):
    """Atomically rename an open file to ``parent``/``name``, replacing a file."""
    _check_component(name)
    cls = _rename_struct(name)
    for klass, flags in ((_FileRenameInformationEx, 0x1 | 0x2), (_FileRenameInformation, 1)):
        info = cls(flags, parent.h, len(name) * ctypes.sizeof(ctypes.c_wchar), name)
        iosb = IO_STATUS_BLOCK()
        status = NtSetInformationFile(raw_handle, ctypes.byref(iosb), ctypes.byref(info), ctypes.sizeof(info), klass)
        if status >= 0:
            return
        if klass == _FileRenameInformationEx and (status & 0xFFFFFFFF) in (0xC0000003, 0xC00000BB, 0xC000000D):
            continue  # Ex class unsupported by this filesystem
        raise _nt_error(status, name)


def delete_handle(raw_handle):
    """Mark an open file for deletion (the handle's own file, never a link target)."""
    for klass, value in ((_FileDispositionInformationEx, 0x1 | 0x2), (_FileDispositionInformation, 1)):
        info = w.ULONG(value)
        iosb = IO_STATUS_BLOCK()
        status = NtSetInformationFile(raw_handle, ctypes.byref(iosb), ctypes.byref(info), ctypes.sizeof(info), klass)
        if status >= 0:
            return
        if klass == _FileDispositionInformationEx and (status & 0xFFFFFFFF) in (0xC0000003, 0xC00000BB, 0xC000000D):
            continue
        raise _nt_error(status)


# --- Owner-only DACLs (chmod 0o700 / 0o600) ---------------------------------

GetCurrentProcess = kernel32.GetCurrentProcess
GetCurrentProcess.restype = w.HANDLE
GetCurrentProcess.argtypes = []
OpenProcessToken = advapi32.OpenProcessToken
OpenProcessToken.restype = w.BOOL
OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
GetTokenInformation = advapi32.GetTokenInformation
GetTokenInformation.restype = w.BOOL
GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
ConvertSidToStringSidW = advapi32.ConvertSidToStringSidW
ConvertSidToStringSidW.restype = w.BOOL
ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
ConvertStringSecurityDescriptorToSecurityDescriptorW = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
GetSecurityDescriptorDacl = advapi32.GetSecurityDescriptorDacl
GetSecurityDescriptorDacl.restype = w.BOOL
GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.BOOL)]
SetSecurityInfo = advapi32.SetSecurityInfo
SetSecurityInfo.restype = w.DWORD
SetSecurityInfo.argtypes = [w.HANDLE, ctypes.c_int, w.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
GetSecurityInfo = advapi32.GetSecurityInfo
GetSecurityInfo.restype = w.DWORD
GetSecurityInfo.argtypes = [w.HANDLE, ctypes.c_int, w.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                            ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
ConvertSecurityDescriptorToStringSecurityDescriptorW = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = w.BOOL
ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, w.DWORD, w.DWORD,
                                                                 ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
LocalFree = kernel32.LocalFree
LocalFree.restype = ctypes.c_void_p
LocalFree.argtypes = [ctypes.c_void_p]

_SID = None


def current_user_sid():
    global _SID
    if _SID is None:
        token = w.HANDLE()
        if not OpenProcessToken(GetCurrentProcess(), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = w.DWORD()
            GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
            buf = ctypes.create_string_buffer(size.value)
            if not GetTokenInformation(token, 1, buf, size, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            sid_ptr = ctypes.c_void_p.from_buffer(buf).value  # TOKEN_USER.User.Sid
            out = ctypes.c_void_p()
            if not ConvertSidToStringSidW(sid_ptr, ctypes.byref(out)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                _SID = ctypes.wstring_at(out.value)
            finally:
                LocalFree(out)
        finally:
            CloseHandle(token)
    return _SID


def private_sddl(directory):
    inherit = 'OICI' if directory else ''
    sid = current_user_sid()
    return f'D:P(A;{inherit};FA;;;{sid})(A;{inherit};FA;;;SY)'


def _handle_sddl(h):
    sd, dacl = ctypes.c_void_p(), ctypes.c_void_p()
    err = GetSecurityInfo(h.h, 1, 0x4, None, None, ctypes.byref(dacl), None, ctypes.byref(sd))
    if err:
        raise ctypes.WinError(err)
    try:
        out = ctypes.c_void_p()
        if not ConvertSecurityDescriptorToStringSecurityDescriptorW(sd, 1, 0x4, ctypes.byref(out), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.wstring_at(out.value)
        finally:
            LocalFree(out)
    finally:
        LocalFree(sd)


def make_private(handle, *, directory):
    """Equivalent of fchmod(0o700/0o600): protected DACL for owner + SYSTEM only."""
    with _open(handle, '', WRITE_DAC | READ_CONTROL, FILE_OPEN, 0, allow_empty=True) as h:
        # Already exactly private: skip, since SetSecurityInfo re-propagates to
        # every child and repeated calls would grow with the scan directory.
        if _handle_sddl(h).replace('D:PAI', 'D:P') == private_sddl(directory):
            return
        sd = ctypes.c_void_p()
        if not ConvertStringSecurityDescriptorToSecurityDescriptorW(private_sddl(directory), 1, ctypes.byref(sd), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            present, defaulted, dacl = w.BOOL(), w.BOOL(), ctypes.c_void_p()
            if not GetSecurityDescriptorDacl(sd, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
                raise ctypes.WinError(ctypes.get_last_error())
            # SE_FILE_OBJECT; DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION
            err = SetSecurityInfo(h.h, 1, 0x4 | 0x80000000, None, None, dacl, None)
            if err:
                raise ctypes.WinError(err)
        finally:
            LocalFree(sd)


def dacl_sddl(path):
    """Read a path's DACL as SDDL (used by tests to verify privacy)."""
    with _open(None, nt_path(os.path.abspath(path)), READ_CONTROL, FILE_OPEN, FILE_OPEN_REPARSE_POINT) as h:
        return _handle_sddl(h)


# --- Processes -----------------------------------------------------------------

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
ERROR_ACCESS_DENIED = 5
OpenProcess = kernel32.OpenProcess
OpenProcess.restype = w.HANDLE
OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
GetExitCodeProcess = kernel32.GetExitCodeProcess
GetExitCodeProcess.restype = w.BOOL
GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
GetProcessTimes = kernel32.GetProcessTimes
GetProcessTimes.restype = w.BOOL
GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4


def process_start(pid):
    """Creation time of a running process in 100 ns ticks; None when no such process runs,
    0 when it runs but cannot be inspected."""
    handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return 0 if ctypes.get_last_error() == ERROR_ACCESS_DENIED else None
    try:
        code = w.DWORD()
        # A handle can outlive its process; an exit code means the process has ended.
        if GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != STILL_ACTIVE:
            return None
        times = [w.FILETIME() for _ in range(4)]
        if not GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return 0
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        CloseHandle(handle)


# --- Job objects (session + rlimits) -----------------------------------------

class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong), ('PerJobUserTimeLimit', ctypes.c_longlong),
                ('LimitFlags', w.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', w.DWORD),
                ('Affinity', ctypes.c_size_t), ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                                                   'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION), ('IoInfo', IO_COUNTERS),
                ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [('TotalUserTime', ctypes.c_longlong), ('TotalKernelTime', ctypes.c_longlong),
                ('ThisPeriodTotalUserTime', ctypes.c_longlong), ('ThisPeriodTotalKernelTime', ctypes.c_longlong),
                ('TotalPageFaultCount', w.DWORD), ('TotalProcesses', w.DWORD),
                ('ActiveProcesses', w.DWORD), ('TotalTerminatedProcesses', w.DWORD)]


class JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [('BasicInfo', JOBOBJECT_BASIC_ACCOUNTING_INFORMATION), ('IoInfo', IO_COUNTERS)]


CreateJobObjectW = kernel32.CreateJobObjectW
CreateJobObjectW.restype = w.HANDLE
CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
SetInformationJobObject = kernel32.SetInformationJobObject
SetInformationJobObject.restype = w.BOOL
SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
QueryInformationJobObject = kernel32.QueryInformationJobObject
QueryInformationJobObject.restype = w.BOOL
QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
AssignProcessToJobObject = kernel32.AssignProcessToJobObject
AssignProcessToJobObject.restype = w.BOOL
AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
TerminateJobObject = kernel32.TerminateJobObject
TerminateJobObject.restype = w.BOOL
TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
TerminateProcess = kernel32.TerminateProcess
TerminateProcess.restype = w.BOOL
TerminateProcess.argtypes = [w.HANDLE, w.UINT]

CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_DEFAULT_ERROR_MODE = 0x04000000
CREATIONFLAGS = CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | CREATE_DEFAULT_ERROR_MODE

JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class Job:
    """A kill-on-close job: the Windows analogue of a POSIX session/process group.

    ``cpu_seconds``  ~ RLIMIT_CPU (per process user time),
    ``memory_bytes`` ~ RLIMIT_AS (per process committed memory),
    ``max_processes`` ~ RLIMIT_NPROC (active processes in the job),
    unhandled exceptions terminate without WER dialogs/dumps ~ RLIMIT_CORE=0.
    """

    def __init__(self, *, cpu_seconds=None, memory_bytes=None, max_processes=None):
        h = CreateJobObjectW(None, None)
        if not h:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = Handle(h)
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        basic = info.BasicLimitInformation
        basic.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        if cpu_seconds is not None:
            basic.LimitFlags |= JOB_OBJECT_LIMIT_PROCESS_TIME
            basic.PerProcessUserTimeLimit = int(cpu_seconds * 10_000_000)
        if memory_bytes is not None:
            basic.LimitFlags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY
            info.ProcessMemoryLimit = memory_bytes
        if max_processes is not None:
            basic.LimitFlags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            basic.ActiveProcessLimit = max_processes
        if not SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
            err = ctypes.get_last_error()
            self.handle.close()
            raise ctypes.WinError(err)

    def adopt(self, proc):
        """Assign a CREATE_SUSPENDED Popen to the job, then let it run."""
        ph = int(proc._handle)
        if not AssignProcessToJobObject(self.handle.h, ph):
            err = ctypes.get_last_error()
            TerminateProcess(ph, 1)
            raise ctypes.WinError(err)
        status = NtResumeProcess(ph)
        if status < 0:
            TerminateProcess(ph, 1)
            raise _nt_error(status)

    def kill(self):
        if self.handle.h:
            TerminateJobObject(self.handle.h, 1)

    def bytes_written(self):
        info = JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION()
        if not QueryInformationJobObject(self.handle.h, 8, ctypes.byref(info), ctypes.sizeof(info), None):
            return 0
        return info.IoInfo.WriteTransferCount

    def active_processes(self):
        info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        if not QueryInformationJobObject(self.handle.h, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            return 0
        return info.ActiveProcesses

    def close(self):
        self.handle.close()  # KILL_ON_JOB_CLOSE terminates any survivor


def spawn_in_job(argv, job, **popen_kwargs):
    import subprocess
    flags = popen_kwargs.pop('creationflags', 0) | CREATIONFLAGS
    proc = subprocess.Popen(argv, creationflags=flags, **popen_kwargs)
    try:
        job.adopt(proc)
    except BaseException:
        proc.wait()
        raise
    return proc


def minimal_env(home):
    """Variables Windows programs need to start, with per-run home/temp dirs."""
    sysroot = os.environ.get('SystemRoot') or os.environ.get('SYSTEMROOT') or r'C:\Windows'
    home = str(home)
    return {'SYSTEMROOT': sysroot, 'WINDIR': sysroot, 'COMSPEC': os.path.join(sysroot, 'System32', 'cmd.exe'),
            'PATHEXT': os.environ.get('PATHEXT', '.COM;.EXE;.BAT;.CMD'),
            'USERPROFILE': home, 'APPDATA': home, 'LOCALAPPDATA': home, 'TEMP': home, 'TMP': home}


def system_path():
    sysroot = os.environ.get('SystemRoot') or r'C:\Windows'
    return os.pathsep.join([os.path.join(sysroot, 'System32'), sysroot])
