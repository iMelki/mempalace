"""Retain an owned lease by exact-handle rename; never delete a lease object.

Windows uses the same no-replace FILE_RENAME_INFO contract as the shared
StorageGovernance NativeFileMetadata.RenameNoReplace primitive. A held handle
denies competing writes/deletes from validation through rename. Explicit POSIX
marker callers retain the legacy path identity check, with no atomicity claim.
"""

from __future__ import annotations

import ctypes
import os
import stat
from pathlib import Path
from typing import Callable


def _same_object(
    current: os.stat_result,
    expected: os.stat_result,
    content_immutable: bool = True,
) -> bool:
    return (
        stat.S_ISREG(current.st_mode)
        and current.st_dev == expected.st_dev
        and current.st_ino == expected.st_ino
        and (
            not content_immutable
            or (current.st_size == expected.st_size and current.st_mtime_ns == expected.st_mtime_ns)
        )
    )


def _rename_handle(handle: int, destination: Path) -> None:
    from ctypes import wintypes

    class RenameInfo(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]

    name = str(destination.absolute()).encode("utf-16-le")
    buffer = ctypes.create_string_buffer(ctypes.sizeof(RenameInfo) + len(name))
    info = RenameInfo.from_buffer(buffer)
    info.Flags = 0  # No replace, including a late competing destination.
    info.RootDirectory = None
    info.FileNameLength = len(name)
    ctypes.memmove(ctypes.addressof(buffer) + RenameInfo.FileName.offset, name, len(name))
    rename = ctypes.WinDLL("kernel32", use_last_error=True).SetFileInformationByHandle
    rename.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    rename.restype = wintypes.BOOL
    if not rename(handle, 3, buffer, len(buffer)):
        raise ctypes.WinError(ctypes.get_last_error())


def _open_release_handle(path: Path) -> int:
    from ctypes import wintypes

    create = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    # GENERIC_READ | DELETE, FILE_SHARE_READ, OPEN_EXISTING, OPEN_REPARSE_POINT.
    handle = create(str(path.absolute()), 0x80010000, 0x1, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def rename_owned_marker(
    path: Path,
    expected: os.stat_result,
    destination: Path,
    *,
    before_rename: Callable[[], None] | None = None,
    content_immutable: bool = True,
) -> None:
    """Move only the exact acquired object and independently read back its identity."""
    if os.name != "nt":
        if not _same_object(path.lstat(), expected, content_immutable):
            raise OSError("maintenance marker ownership changed; foreign marker preserved")
        if destination.exists():
            raise FileExistsError("maintenance release destination already exists")
        # POSIX compatibility only; hostile path replacement remains unproved.
        path.rename(destination)
        return

    import msvcrt

    handle = _open_release_handle(path)
    descriptor = None
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        observed = os.fstat(descriptor)
        if not _same_object(observed, expected, content_immutable) or (
            getattr(observed, "st_file_attributes", 0) & 0x400
        ):
            raise OSError("maintenance marker ownership changed; foreign marker preserved")
        if before_rename:
            before_rename()
        _rename_handle(handle, destination)
        if not _same_object(destination.lstat(), observed):
            raise OSError("maintenance marker release readback identity differs")
        # Destination readback proves this object's release. A new owner may
        # immediately use the old name; do not inspect or mutate its pathname.
    finally:
        if descriptor is not None:
            os.close(descriptor)
        else:
            close = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
            close.argtypes = [ctypes.c_void_p]
            close(handle)
