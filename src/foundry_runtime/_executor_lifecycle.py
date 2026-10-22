"""Private Windows executor custody; no CLI and no permission/sandbox claim.

Job membership is assigned atomically by CreateProcess, not after launch. Jobs
cover inherited Win32 descendants, not work delegated to WMI/WSL/services.
Leases require a local NTFS volume and cooperating dispatchers; stale locks are
never reclaimed automatically. The legacy ``pid`` field remains a read interface
for worktree_busy_check.lock_map; pid_deprecated is a compatibility marker, not
permission to remove that occupancy interface without migrating its reader.
"""

from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
from dataclasses import dataclass
import json
import io
import math
import os
from pathlib import Path
import subprocess
import time
import uuid


class _STARTUPINFO(c.Structure):
    _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR),
                ("lpDesktop", w.LPWSTR), ("lpTitle", w.LPWSTR),
                *[(n, w.DWORD) for n in ("dwX", "dwY", "dwXSize", "dwYSize",
                  "dwXCountChars", "dwYCountChars", "dwFillAttribute", "dwFlags")],
                ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
                ("lpReserved2", c.c_void_p), ("hStdInput", w.HANDLE),
                ("hStdOutput", w.HANDLE), ("hStdError", w.HANDLE)]


class _STARTUPINFOEX(c.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFO), ("lpAttributeList", c.c_void_p)]


class _PROCESS_INFORMATION(c.Structure):
    _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE),
                ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]


class _LIMIT(c.Structure):
    _fields_ = [("PerProcessUserTimeLimit", c.c_int64),
                ("PerJobUserTimeLimit", c.c_int64), ("LimitFlags", w.DWORD),
                ("MinimumWorkingSetSize", c.c_size_t),
                ("MaximumWorkingSetSize", c.c_size_t), ("ActiveProcessLimit", w.DWORD),
                ("Affinity", c.c_size_t), ("PriorityClass", w.DWORD),
                ("SchedulingClass", w.DWORD)]


class _IO(c.Structure):
    _fields_ = [(n, c.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _EXTENDED_LIMIT(c.Structure):
    _fields_ = [("BasicLimitInformation", _LIMIT), ("IoInfo", _IO),
                *[(n, c.c_size_t) for n in ("ProcessMemoryLimit", "JobMemoryLimit",
                  "PeakProcessMemoryUsed", "PeakJobMemoryUsed")]]


class _ACCOUNTING(c.Structure):
    _fields_ = [(n, c.c_int64) for n in ("TotalUserTime", "TotalKernelTime",
                "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")] + [
                (n, w.DWORD) for n in ("TotalPageFaultCount", "TotalProcesses",
                "ActiveProcesses", "TotalTerminatedProcesses")]


def _api():
    if os.name != "nt":
        raise OSError("Executor custody requires Windows 10 or later")
    api = c.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CloseHandle": ([w.HANDLE], w.BOOL),
        "CreateJobObjectW": ([c.c_void_p, w.LPCWSTR], w.HANDLE),
        "SetInformationJobObject": ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD], w.BOOL),
        "QueryInformationJobObject": ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p], w.BOOL),
        "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
        "IsProcessInJob": ([w.HANDLE, w.HANDLE, c.POINTER(w.BOOL)], w.BOOL),
        "GetCurrentProcess": ([], w.HANDLE),
        "ReadFile": ([w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p], w.BOOL),
        "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        "GetProcessTimes": ([w.HANDLE] + [c.POINTER(w.FILETIME)] * 4, w.BOOL),
        "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
        "GetExitCodeProcess": ([w.HANDLE, c.POINTER(w.DWORD)], w.BOOL),
        "DuplicateHandle": ([w.HANDLE, w.HANDLE, w.HANDLE, c.POINTER(w.HANDLE),
                             w.DWORD, w.BOOL, w.DWORD], w.BOOL),
        "InitializeProcThreadAttributeList": ([c.c_void_p, w.DWORD, w.DWORD,
                                                c.POINTER(c.c_size_t)], w.BOOL),
        "UpdateProcThreadAttribute": ([c.c_void_p, w.DWORD, c.c_size_t, c.c_void_p,
                                       c.c_size_t, c.c_void_p, c.c_void_p], w.BOOL),
        "DeleteProcThreadAttributeList": ([c.c_void_p], None),
        "CreateProcessW": ([w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p, w.BOOL,
                            w.DWORD, c.c_void_p, w.LPCWSTR, c.c_void_p,
                            c.POINTER(_PROCESS_INFORMATION)], w.BOOL),
        "GetVolumePathNameW": ([w.LPCWSTR, w.LPWSTR, w.DWORD], w.BOOL),
        "GetDriveTypeW": ([w.LPCWSTR], w.UINT),
        "GetVolumeInformationW": ([w.LPCWSTR, w.LPWSTR, w.DWORD, c.c_void_p,
                                   c.c_void_p, c.c_void_p, w.LPWSTR, w.DWORD], w.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    return api


def _check(result):
    if not result:
        raise c.WinError(c.get_last_error())
    return result


def observe_locked_byte(path):
    """Read the existing controller lock without acquiring it or writing bytes.

    Windows mandatory byte locking rejects ReadFile with ERROR_LOCK_VIOLATION.
    Access-denied or absent evidence is unknown, never proof of a dead owner.
    """
    try:
        import msvcrt
        api = _api()
        with Path(path).open('rb', buffering=0) as stream:
            before = os.fstat(stream.fileno()).st_mtime_ns
            byte, count = c.create_string_buffer(1), w.DWORD()
            if api.ReadFile(msvcrt.get_osfhandle(stream.fileno()), byte, 1, c.byref(count), None):
                state = 'released' if count.value == 1 else 'unobserved'
            elif c.get_last_error() == 33:
                state = 'held'
            else:
                raise c.WinError(c.get_last_error())
            if os.fstat(stream.fileno()).st_mtime_ns != before:
                state = 'unobserved'
        return {'state': state, 'generation': before, 'path': str(path)}
    except (OSError, ImportError) as exc:
        return {'state': 'unobserved', 'path': str(path), 'reason': str(exc)}


def _creation(api, handle):
    fields = [w.FILETIME() for _ in range(4)]
    _check(api.GetProcessTimes(handle, *(c.byref(f) for f in fields)))
    return (fields[0].dwHighDateTime << 32) | fields[0].dwLowDateTime


def _seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("timeout must be a finite nonnegative number")
    return float(value)


def _local_ntfs(path):
    if str(path).startswith(("\\\\", "//")):
        raise ValueError("Executor leases require local NTFS; network paths are unsupported")
    resolved = path.resolve(strict=True)
    api = _api()
    root, filesystem = c.create_unicode_buffer(32768), c.create_unicode_buffer(64)
    _check(api.GetVolumePathNameW(str(resolved), root, len(root)))
    _check(api.GetVolumeInformationW(root.value, None, 0, None, None, None, filesystem, len(filesystem)))
    if api.GetDriveTypeW(root.value) != 3 or filesystem.value.upper() != "NTFS":
        raise ValueError("Executor leases require a local fixed NTFS volume")
    return resolved


@dataclass(frozen=True)
class CleanupReceipt:
    attempt_id: str
    root_pid: int | None
    root_creation_time: int | None
    root_exited: bool
    job_active_processes: int | None
    cleanup_verified: bool
    reason: str


class SpawnFailure(OSError):
    """A child was created; callers must not assume a no-spawn failure.

    ``process`` retains custody if cleanup is unknown. An unbound lease remains
    reserved until the caller reconciles this receipt; do not invent a no-spawn
    receipt from the exception's generic OSError base class.
    """

    def __init__(self, cause, process, cleanup):
        super().__init__(str(cause))
        self.spawn_started = True
        self.process = process
        self.cleanup = cleanup


def process_identity_exited(pid, creation_time):
    """Observe a particular Windows process, treating PID reuse as a different process."""
    api = _api()
    handle = api.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        error = c.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no process has this PID.
            return True
        raise c.WinError(error)
    try:
        if creation_time is not None and _creation(api, handle) != creation_time:
            return True
        status = api.WaitForSingleObject(handle, 0)
        if status not in (0, 258):
            raise c.WinError(c.get_last_error())
        return status == 0
    finally:
        api.CloseHandle(handle)


def measure_abandoned(identity, *, roles=('owner', 'root')):
    """Observe an abandoned owned Job; never terminate a process to obtain a receipt.

    identity holds attempt_id and the owner/root pid and creation time, read from the
    executor lock or, when the controller released it, from the intent events.

    OwnedProcess creates a non-inherited KILL_ON_JOB_CLOSE Job with no breakaway.
    When its controller exits, Windows terminates its descendants. Check both
    recorded identities and the process tree before reporting that cleanup.
    https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects

    roles=('root',) is for a native call whose process.json records only its root:
    the caller must already hold proof that the controller exited (a released
    controller lock), since this observation cannot see the controller.
    """
    row = identity
    if roles not in (('owner', 'root'), ('root',)):
        raise ValueError('abandoned observation roles must be owner+root or root')
    roots = [(row[k + '_pid'], row[k + '_creation_time']) for k in roles]
    if any(type(v) is not int or v <= 0 for pair in roots for v in pair):
        raise ValueError('abandoned observation requires recorded ' + ' and '.join(roles) + ' identities')
    for pid, created in roots:
        if not process_identity_exited(pid, created):
            raise RuntimeError('recorded process is still running: ' + str(pid))
    script = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding="
              "[System.Text.UTF8Encoding]::new($false); "
              "@(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,"
              "@{n='Created';e={if ($_.CreationDate) {$_.CreationDate.ToFileTimeUtc()} else {0}}}) "
              "| ConvertTo-Json -Compress")
    result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, encoding='utf-8', errors='replace', timeout=60, check=True)
    processes = json.loads(result.stdout)
    if not isinstance(processes, list) or not processes:
        raise RuntimeError('process observation did not return a process table')
    by_pid = {p['ProcessId']: p for p in processes}
    remnants = []
    for process in processes:
        current, seen = process, set()
        while current['ProcessId'] not in seen:
            seen.add(current['ProcessId'])
            parent_pid = current['ParentProcessId']
            parent = by_pid.get(parent_pid)
            for pid, created in roots:
                # A newer incarnation of the parent PID does not belong to this attempt.
                if (parent_pid == pid and current['Created'] >= created
                        and (parent is None or parent['Created'] > current['Created'])):
                    if not process_identity_exited(process['ProcessId'], None):
                        remnants.append(process['ProcessId'])
            if parent is None or parent['Created'] > current['Created']:
                break
            current = parent
    if remnants:
        raise RuntimeError('abandoned attempt still has live descendants: ' + str(sorted(set(remnants))))
    # Recheck identities after enumeration; no process mutation has occurred.
    if not all(process_identity_exited(pid, created) for pid, created in roots):
        raise RuntimeError('recorded process remains active after observation')
    cleanup = CleanupReceipt(row['attempt_id'], row['root_pid'], row['root_creation_time'],
        True, 0, True, ('Controller and root identities exited' if 'owner' in roles
                        else 'Root identity exited after its controller released the round lock')
        + '; owned kill-on-close Job and descendant observation establish cleanup')
    observation = {'method': 'owned-job-controller-exit-and-process-tree',
                   **{k + suffix: row[k + suffix] for k in roles for suffix in ('_pid', '_creation_time')},
                   'observed_processes': len(processes), 'live_descendants': [],
                   'measured_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    return cleanup, observation


def _exclude_executor_lock(worktree):
    """Keep runtime occupancy out of Git, including linked worktrees."""
    import msvcrt

    result = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, encoding="utf-8", check=False)
    if result.returncode:
        return  # Leases also support non-repository execution directories.
    exclude = Path(result.stdout.strip()) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a+b") as stream:
        # Sibling worktrees share this file. Serialize only its read/append so
        # simultaneous first launches cannot append duplicate entries.
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        try:
            content = stream.read()
            if b".pdcl-executor.lock" not in content.splitlines():
                stream.seek(0, os.SEEK_END)
                stream.write((b"\n" if content and not content.endswith(b"\n") else b"")
                             + b".pdcl-executor.lock\n")
                stream.flush()
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


class ExecutorLease:
    def __init__(self, path, payload):
        self.path, self.attempt_id = path, payload["attempt_id"]
        self._payload = payload

    @classmethod
    def acquire(cls, worktree: Path, tag: str, launcher: str):
        worktree = _local_ntfs(Path(worktree))
        _exclude_executor_lock(worktree)
        api = _api()
        owner_pid = os.getpid()
        owner_time = _creation(api, api.GetCurrentProcess())
        payload = dict(schema_version="pdcl-executor-lock-2", attempt_id=str(uuid.uuid4()),
                       state="reserved", owner_pid=owner_pid, owner_creation_time=owner_time,
                       root_pid=None, root_creation_time=None, pid=owner_pid,
                       pid_deprecated=True, worktree=str(worktree), tag=tag, launcher=launcher,
                       started=time.strftime("%Y-%m-%dT%H:%M:%S"))
        path = worktree / ".pdcl-executor.lock"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        # A partial record on IO failure remains blocking, rather than pretending
        # that acquisition and cleanup succeeded.
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        return cls(path, payload)

    def _ours(self):
        try:
            row = json.loads(self.path.read_text(encoding="utf-8"))
            return isinstance(row, dict) and row.get("attempt_id") == self.attempt_id
        except (OSError, ValueError):
            return False

    def require_reserved(self, worktree: Path, tag: str, launchers) -> None:
        """Admit only this owner's unchanged reservation for this exact launch.

        Both transports call this authority before evidence or native creation.
        A lease from another tree, owner, state or attempt is never transferable.
        ``launchers`` is an explicit finite collection, not substring matching.
        """
        if (not isinstance(launchers, (tuple, list, set, frozenset)) or not launchers
                or any(not isinstance(value, str) or not value for value in launchers)):
            raise ValueError("allowed launchers must be a nonempty collection of strings")
        requested = _local_ntfs(Path(worktree))
        try:
            disk = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError("executor reservation is missing or unreadable") from error
        api = _api()
        owner_time = _creation(api, api.GetCurrentProcess())
        if (not isinstance(disk, dict) or disk != self._payload
                or self.path != requested / ".pdcl-executor.lock"
                or disk.get("schema_version") != "pdcl-executor-lock-2"
                or disk.get("attempt_id") != self.attempt_id
                or disk.get("state") != "reserved"
                or disk.get("root_pid") is not None or disk.get("root_creation_time") is not None
                or disk.get("worktree") != str(requested) or disk.get("tag") != tag
                or disk.get("launcher") not in launchers
                or disk.get("owner_pid") != os.getpid()
                or disk.get("owner_creation_time") != owner_time):
            raise RuntimeError("executor requires this worktree/tag/owner's unchanged reserved lease")

    def _update(self, **fields):
        if not self._ours():
            raise RuntimeError("executor lease identity changed")
        self._payload.update(fields)
        with self.path.open("r+", encoding="utf-8") as stream:
            row = json.load(stream)
            if row.get("attempt_id") != self.attempt_id:
                raise RuntimeError("executor lease identity changed")
            stream.seek(0)
            json.dump(self._payload, stream)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())

    def bind_process(self, pid: int, creation_time: int):
        if self._payload["root_pid"] is not None:
            raise RuntimeError("executor lease already bound")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or not isinstance(creation_time, int) or creation_time <= 0:
            raise ValueError("root PID and creation time must be positive integers")
        self._update(state="running", root_pid=pid, root_creation_time=creation_time, pid=pid)

    def retain_unknown(self, reason: str):
        self._update(state="cleanup_unknown", cleanup_reason=reason)

    def release(self, cleanup: CleanupReceipt) -> bool:
        if not self._ours() or cleanup.attempt_id != self.attempt_id:
            return False
        identity_matches = (cleanup.root_pid == self._payload["root_pid"] and
                            cleanup.root_creation_time == self._payload["root_creation_time"])
        if not identity_matches:
            return False
        if not cleanup.cleanup_verified or not cleanup.root_exited or cleanup.job_active_processes != 0:
            self.retain_unknown(cleanup.reason)
            return False
        self.path.unlink()
        return True


def _new_job(api):
    handle = _check(api.CreateJobObjectW(None, None))
    try:
        limits = _EXTENDED_LIMIT()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE only
        _check(api.SetInformationJobObject(handle, 9, c.byref(limits), c.sizeof(limits)))
        return handle
    except BaseException:
        api.CloseHandle(handle)
        raise


def _create_process(api, executable, command, env_block, cwd, startup, info):
    # The custodian owns this output record before entering Win32. Even an
    # exception immediately after CreateProcess returns cannot hide live handles.
    _check(api.CreateProcessW(executable, command, None, None, True,
                             0x00080000 | 0x08000000 | 0x00000400,
                             env_block, cwd, c.byref(startup), c.byref(info)))


class OwnedProcess:
    @property
    def pid(self):
        return self._info.dwProcessId

    @property
    def _handle(self):
        return self._info.hProcess

    @_handle.setter
    def _handle(self, value):
        self._info.hProcess = value

    @classmethod
    def spawn(cls, argv, *, cwd, stdin, stdout, stderr, attempt_id, env=None, cleanup_path=None):
        import msvcrt

        if not argv or not attempt_id:
            raise ValueError("argv and attempt_id are required")
        executable = Path(argv[0])
        if not executable.is_absolute() or executable.suffix.lower() != ".exe":
            raise ValueError("OwnedProcess requires an absolute native .exe; batch shims are unsupported")
        api = _api()
        # Allocate and fully initialize the custodian before any native child or
        # Job handle exists. The native process output is already owned storage.
        process = cls()
        info = _PROCESS_INFORMATION()
        process._api, process._info, process._job = api, info, None
        process.creation_time = None
        process.attempt_id, process.argv = attempt_id, argv
        process.stdin = process.stdout = process.stderr = None
        process._streams, process._receipt = [], None
        job = _new_job(api)
        process._job = job
        streams, inherited = process._streams, []
        attributes = None
        attributes_ready = False

        def duplicate(descriptor):
            handle = w.HANDLE()
            current = api.GetCurrentProcess()
            _check(api.DuplicateHandle(current, msvcrt.get_osfhandle(descriptor),
                                       current, c.byref(handle), 0, True, 2))
            inherited.append(handle.value)
            return handle.value

        def stdio(spec, input_stream=False):
            if spec == subprocess.PIPE:
                read_fd, write_fd = os.pipe()
                child_fd, parent_fd = (read_fd, write_fd) if input_stream else (write_fd, read_fd)
                try:
                    handle = duplicate(child_fd)
                    stream = io.TextIOWrapper(
                        os.fdopen(parent_fd, "wb" if input_stream else "rb", buffering=0),
                        encoding="utf-8", errors="replace", write_through=True,
                    )
                except BaseException:
                    os.close(parent_fd)
                    raise
                finally:
                    os.close(child_fd)
                streams.append(stream)
                return handle, stream
            if spec is None or spec == subprocess.DEVNULL:
                with open(os.devnull, "rb" if input_stream else "wb") as stream:
                    return duplicate(stream.fileno()), None
            return duplicate(spec if isinstance(spec, int) else spec.fileno()), None

        try:
            hin, pin = stdio(stdin, True)
            hout, pout = stdio(stdout)
            herr, perr = (hout, None) if stderr == subprocess.STDOUT else stdio(stderr)
            size = c.c_size_t()
            api.InitializeProcThreadAttributeList(None, 2, 0, c.byref(size))
            if not size.value:
                raise c.WinError(c.get_last_error())
            attributes = c.create_string_buffer(size.value)
            _check(api.InitializeProcThreadAttributeList(attributes, 2, 0, c.byref(size)))
            attributes_ready = True
            jobs = (w.HANDLE * 1)(job)
            handles = (w.HANDLE * len(inherited))(*inherited)
            _check(api.UpdateProcThreadAttribute(attributes, 0, 0x0002000D,
                                                jobs, c.sizeof(jobs), None, None))
            _check(api.UpdateProcThreadAttribute(attributes, 0, 0x00020002,
                                                handles, c.sizeof(handles), None, None))
            startup = _STARTUPINFOEX()
            startup.StartupInfo.cb = c.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = hin
            startup.StartupInfo.hStdOutput = hout
            startup.StartupInfo.hStdError = herr
            startup.lpAttributeList = c.cast(attributes, c.c_void_p)
            child_env = dict(os.environ if env is None else env)
            if cleanup_path is not None:
                # Descendant resource owners may reuse this custodian's existing
                # final receipt if outer-Job termination also ends their observer.
                child_env['PDCL_EXECUTOR_CLEANUP'] = json.dumps({
                    'path': str(Path(cleanup_path).resolve()), 'attempt_id': attempt_id})
            if any(not isinstance(k, str) or not isinstance(v, str) or "\0" in k + v or "=" in k for k, v in child_env.items()):
                raise ValueError("invalid child environment")
            block = c.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in
                                             sorted(child_env.items(), key=lambda pair: pair[0].upper())) + "\0\0")
            command = c.create_unicode_buffer(subprocess.list2cmdline([str(a) for a in argv]))
            process.stdin, process.stdout, process.stderr = pin, pout, perr
            _create_process(api, str(executable), command, block, str(cwd), startup, info)
            process.creation_time = _creation(api, info.hProcess)
            return process
        except BaseException as error:
            if info.hProcess:
                receipt = process.stop_and_verify(drain_seconds=0, kill_seconds=2)
                raise SpawnFailure(error, process, receipt) from error
            for stream in streams:
                try:
                    stream.close()
                except OSError:
                    pass
            api.CloseHandle(job)
            raise
        finally:
            if info.hThread:
                api.CloseHandle(info.hThread)
            for handle in inherited:
                api.CloseHandle(handle)
            if attributes_ready:
                api.DeleteProcThreadAttributeList(attributes)

    def poll(self):
        if self._handle is None:
            return self._returncode
        status = self._api.WaitForSingleObject(self._handle, 0)
        if status == 258:
            return None
        if status != 0:
            raise c.WinError(c.get_last_error())
        result = w.DWORD()
        _check(self._api.GetExitCodeProcess(self._handle, c.byref(result)))
        return result.value

    def wait(self, timeout):
        deadline = time.monotonic() + _seconds(timeout)
        while True:
            rc = self.poll()
            if rc is not None:
                return rc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(self.argv, timeout)
            time.sleep(min(0.02, remaining))

    def contains(self, pid: int, creation_time: int) -> bool:
        if self._job is None:
            return False
        handle = self._api.OpenProcess(0x1000, False, pid)
        if not handle:
            if c.get_last_error() == 87:  # process no longer exists
                return False
            raise c.WinError(c.get_last_error())
        try:
            if _creation(self._api, handle) != creation_time:
                return False
            answer = w.BOOL()
            _check(self._api.IsProcessInJob(handle, self._job, c.byref(answer)))
            return bool(answer.value)
        finally:
            self._api.CloseHandle(handle)

    def _active(self):
        accounting = _ACCOUNTING()
        _check(self._api.QueryInformationJobObject(self._job, 1, c.byref(accounting),
                                                  c.sizeof(accounting), None))
        return accounting.ActiveProcesses

    def stop_and_verify(self, *, drain_seconds, kill_seconds):
        """Passive drain (no signal), then terminate this Job and poll to deadline.

        Unknown cleanup leaves the Job/process handles owned and can be retried.
        Callers must not release their lease or claim success on that receipt.
        """
        drain_seconds, kill_seconds = _seconds(drain_seconds), _seconds(kill_seconds)
        if self._receipt and self._receipt.cleanup_verified:
            return self._receipt
        active, root_exited, reason = None, False, "cleanup deadline expired"
        try:
            deadline = time.monotonic() + drain_seconds
            while True:
                root_exited, active = self.poll() is not None, self._active()
                if root_exited and active == 0:
                    break
                if time.monotonic() >= deadline:
                    _check(self._api.TerminateJobObject(self._job, 124))
                    deadline = time.monotonic() + kill_seconds
                    while True:
                        root_exited, active = self.poll() is not None, self._active()
                        if root_exited and active == 0 or time.monotonic() >= deadline:
                            break
                        time.sleep(min(0.02, max(0, deadline - time.monotonic())))
                    break
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
            if root_exited and active == 0:
                reason = "root exited and owned Job has zero active processes"
        except OSError as error:
            reason = "cleanup measurement unavailable: " + str(error)
            active = None
            # Still request owned termination; failed measurement is not permission
            # to leave active user code running or to release the occupancy record.
            self._api.TerminateJobObject(self._job, 124)
        verified = root_exited and active == 0
        receipt = CleanupReceipt(self.attempt_id, self.pid, self.creation_time,
                                 root_exited, active, verified, reason)
        if verified:
            self._returncode = self.poll()
            # Pipe streams are raw/write-through, so close has no buffered input
            # to push into a dead reader. Broken pipes must not leak Job handles.
            close_errors = []
            try:
                for stream in self._streams:
                    try:
                        stream.close()
                    except OSError as error:
                        close_errors.append(str(error))
            finally:
                self._api.CloseHandle(self._handle)
                self._api.CloseHandle(self._job)
                self._handle = self._job = None
            if close_errors:
                receipt = CleanupReceipt(self.attempt_id, self.pid, self.creation_time,
                                         True, 0, True, reason + "; stream close: " + "; ".join(close_errors))
        self._receipt = receipt
        return receipt
