"""Target-file checks for editing an unloaded world while the game stays open."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def assert_world_available(world: Path) -> None:
    """Probe only this world, never prohibit an unrelated running game process.

    This is a point-in-time availability check, not a claim to know a player's
    location. The player must remain away until the atomic edit finishes.
    """
    world = Path(world).resolve(strict=True)
    if not world.is_file():
        raise ValueError(f"Not a world file: {world}")
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            create = kernel.CreateFileW
            create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                               wintypes.HANDLE)
            create.restype = wintypes.HANDLE
            close = kernel.CloseHandle
            close.argtypes = (wintypes.HANDLE,)
            close.restype = wintypes.BOOL
            # OPEN_EXISTING, no sharing: fail if the engine still has this
            # world open. Do not create, truncate, or write the target file.
            handle = create(str(world), 0x80000000 | 0x40000000, 0, None, 3, 0, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            close(handle)
        else:
            import fcntl
            with world.open("r+b") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise RuntimeError(
            f"Target .world is still in use or is not writable: {world}\n"
            "Leave this planet and wait for it to unload; other players must "
            "also leave it. You do not need to close the whole game. "
            f"Do not return until editing finishes. ({exc})"
        ) from exc


def fingerprint(world: Path) -> dict:
    world = Path(world)
    before = world.stat()
    digest = hashlib.sha256()
    with world.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = world.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError("Target .world changed while being read; retry after it unloads")
    return {"size": after.st_size, "mtimeNs": after.st_mtime_ns, "sha256": digest.hexdigest()}


def assert_world_unchanged(world: Path, expected: dict) -> None:
    assert_world_available(world)
    if fingerprint(world) != expected:
        raise RuntimeError(
            "Target .world changed during editing; the result was not installed. "
            "Leave the planet, wait for it to unload, and retry."
        )
