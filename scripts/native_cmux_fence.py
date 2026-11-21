"""Read-only rename fences. No process signals, polling retries or filesystem writes.

ctime on leaves does not detect directory ABA. Observe rename/delete/revoke on
EVERY resolved ancestor while a pathname consumer runs. Content changes in global
/tmp or HOME produce WRITE, not RENAME; they must not invalidate this fence.
Snapshots still check content, hardlinks, modes and internal directory identities.
Unsupported kernels and event-queue overflow fail closed.
"""
from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import select
import stat
import struct
import sys


@contextmanager
def directory_fences():
    """Arm every distinct physical directory once for a single input owner."""
    fds, paths, watcher = {}, {}, None
    failed = False

    def check_identity(entry, fd, phase):
        a, b = entry.stat(follow_symlinks=False), os.fstat(fd)
        if not stat.S_ISDIR(a.st_mode) or (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            raise ValueError("Directory ancestor identity changed " + phase)

    def bind(path):
        nonlocal failed
        try:
            path = Path(path).absolute()
            canonical = path.resolve(strict=True)
            directory = canonical if canonical.is_dir() else canonical.parent
            chain = [*reversed(directory.parents), directory]
            for entry in chain:
                if entry in fds:
                    continue  # Already armed, never close/rearm an ancestor.
                parent_fd = fds.get(entry.parent)
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                fd = os.open(entry.name if parent_fd is not None else str(entry), flags,
                             dir_fd=parent_fd)
                fds[entry] = fd
                if sys.platform == "darwin":
                    event = select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                                         flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                         fflags=select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE | select.KQ_NOTE_REVOKE)
                    watcher.control([event], 0, 0)
                else:
                    # SELF events only, not child CREATE/DELETE/MOVE or directory WRITE.
                    wd = libc.inotify_add_watch(watcher, os.fsencode(f"/proc/self/fd/{fd}"),
                                               0x800 | 0x400 | 0x2000 | 0x4000)
                    if wd < 0:
                        raise OSError(ctypes.get_errno(), "Cannot watch directory ancestor")
            # Close setup races before the first consumer, even on reused ancestors.
            for entry in chain:
                check_identity(entry, fds[entry], "while arming rename fence")
            if path.resolve(strict=True) != canonical or paths.setdefault(path, canonical) != canonical:
                raise ValueError("Input path changed while arming rename fence")
        except BaseException:
            failed = True
            raise

    def check_events():
        if sys.platform == "darwin":
            changed = bool(watcher.control(None, 1, 0))
        else:
            try:
                data = os.read(watcher, 65536)
            except BlockingIOError:
                data = b""
            changed = False
            offset = 0
            while offset < len(data):
                _, mask, _, size = struct.unpack_from("iIII", data, offset)
                changed |= bool(mask & (0x800 | 0x400 | 0x2000 | 0x4000 | 0x8000))
                offset += 16 + size
        if changed:
            raise ValueError("Directory/ancestor rename, deletion, revoke or event overflow during pathname consumption")

    try:
        if sys.platform == "darwin":
            watcher = select.kqueue()
        elif sys.platform.startswith("linux"):
            libc = ctypes.CDLL(None, use_errno=True)
            libc.inotify_init1.argtypes = [ctypes.c_int]
            libc.inotify_init1.restype = ctypes.c_int
            libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            libc.inotify_add_watch.restype = ctypes.c_int
            watcher = libc.inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK)
            if watcher < 0:
                raise OSError(ctypes.get_errno(), "Cannot open directory rename fence")
        else:
            raise ValueError("No supported lossless directory rename fence on this kernel")
        yield bind
        if failed:
            raise ValueError("Incomplete directory rename fence")
        check_events()
        for entry, fd in fds.items():
            check_identity(entry, fd, "during pathname consumption")
        for path, canonical in paths.items():
            if path.resolve(strict=True) != canonical:
                raise ValueError("Input path changed during pathname consumption")
        check_events()  # Coverage extends through all binding rechecks.
    finally:
        if watcher is not None:
            if isinstance(watcher, int):
                if watcher >= 0:
                    os.close(watcher)
            else:
                watcher.close()
        for fd in reversed(fds.values()):
            os.close(fd)


@contextmanager
def directory_fence(path):
    with directory_fences() as bind:
        bind(path)
        yield


def fenced(function):
    """Keep the bundle's complete ancestor chain fenced for a whole operation."""
    from functools import wraps

    @wraps(function)
    def checked(app, *args, **kwargs):
        with directory_fence(app):
            return function(app, *args, **kwargs)
    return checked
