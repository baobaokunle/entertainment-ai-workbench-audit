"""Process-local guard, not an OS sandbox or a detector of real identities."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys


class BoundaryError(ValueError):
    pass


ROOT = Path(__file__).resolve().parents[2]
_installed = False
STATS = {"network_blocked": 0, "process_blocked": 0, "file_blocked": 0,
         "writes": set(), "reads": set()}


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def safe_path(value: str | Path, area: str = "", *, existing: bool = False) -> Path:
    """Reject lexical escapes and every existing link/reparse/hardlink component."""
    base = ROOT / area
    p = Path(value)
    if not p.is_absolute():
        p = base / p
    p = Path(os.path.abspath(p))
    if not inside(p, base):
        raise BoundaryError("PATH_OUTSIDE_ALLOWED_AREA")
    current = ROOT
    for part in p.relative_to(ROOT).parts:
        current = current / part
        if current.exists() or current.is_symlink():
            s = current.lstat()
            if current.is_symlink() or getattr(s, "st_file_attributes", 0) & 0x400:
                raise BoundaryError("LINK_OR_REPARSE_REJECTED")
            if stat.S_ISREG(s.st_mode) and s.st_nlink > 1:
                raise BoundaryError("HARDLINK_REJECTED")
    if not inside(p.resolve(), base.resolve()):
        raise BoundaryError("RESOLVED_PATH_OUTSIDE_ALLOWED_AREA")
    if existing and not p.is_file():
        raise BoundaryError("INPUT_FILE_NOT_FOUND")
    return p


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    stdlib = Path(sys.base_prefix).resolve()

    def audit(event, args):
        if event.startswith("socket."):
            STATS["network_blocked"] += 1
            raise BoundaryError("NETWORK_DISABLED_IN_S1")
        if event in {"subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.startfile", "os.startfile/2", "_winapi.CreateProcess"}:
            STATS["process_blocked"] += 1
            raise BoundaryError("CHILD_PROCESS_DISABLED_IN_S1")
        if event == "sqlite3.connect":
            check_database_paths(args[0])
        if event == "open":
            filename, mode, flags = args
            if isinstance(filename, int):
                return
            p = Path(os.path.abspath(filename))
            write = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            try:
                if inside(p, ROOT):
                    safe_path(p)
                    if write and not any(inside(p, ROOT / a) for a in ("runtime", "results")):
                        raise BoundaryError("WRITE_OUTSIDE_RUNTIME_RESULTS")
                    STATS["writes" if write else "reads"].add(p.relative_to(ROOT).as_posix())
                elif not write and inside(p, stdlib):
                    # Only the explicitly selected CPython base, never A's business modules.
                    return
                else:
                    raise BoundaryError("FILE_OUTSIDE_B_OR_READONLY_PYTHON")
            except BoundaryError:
                STATS["file_blocked"] += 1
                raise
        if event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.link", "os.symlink", "os.chmod", "os.utime", "os.truncate"}:
            paths = args[:2] if event in {"os.rename", "os.link", "os.symlink"} else args[:1]
            for filename in paths:
                if not isinstance(filename, (str, bytes, os.PathLike)):
                    continue
                p = safe_path(os.fsdecode(filename))
                if not any(inside(p, ROOT / a) for a in ("runtime", "results")):
                    raise BoundaryError("MUTATION_OUTSIDE_RUNTIME_RESULTS")

    sys.addaudithook(audit)


def check_database_paths(value):
    path = safe_path(value, "runtime")
    for suffix in ("-journal", "-wal", "-shm"):
        safe_path(str(path) + suffix, "runtime")
    return path


def snapshot() -> dict:
    return {k: sorted(v) if isinstance(v, set) else v for k, v in STATS.items()}
