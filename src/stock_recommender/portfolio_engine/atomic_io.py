"""Crash-safe, rollback-capable file replacement primitives."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class OriginalFile:
    existed: bool
    data: bytes | None


def capture_original(path: Path) -> OriginalFile:
    try:
        return OriginalFile(existed=True, data=path.read_bytes())
    except FileNotFoundError:
        return OriginalFile(existed=False, data=None)


def fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_fsync_temp(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def replace_path(source: Path, target: Path) -> None:
    os.replace(source, target)


def restore_original(path: Path, original: OriginalFile) -> None:
    if original.existed:
        if original.data is None:
            raise OSError(f"missing original bytes for {path}")
        temporary = write_fsync_temp(path, original.data)
        try:
            replace_path(temporary, path)
            fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return
    path.unlink(missing_ok=True)
    fsync_directory(path.parent)


def atomic_replace_many(
    replacements: Mapping[Path, bytes],
    *,
    originals: Mapping[Path, OriginalFile] | None = None,
) -> None:
    """Replace all targets as one rollback-capable filesystem transaction."""

    if not replacements:
        return
    ordered = tuple((Path(path), data) for path, data in replacements.items())
    captured = (
        {path: capture_original(path) for path, _ in ordered}
        if originals is None
        else {path: originals[path] for path, _ in ordered}
    )
    temporaries: dict[Path, Path] = {}
    replacement_started = False
    try:
        for path, data in ordered:
            temporaries[path] = write_fsync_temp(path, data)
        for path, _ in ordered:
            replacement_started = True
            replace_path(temporaries[path], path)
        for directory in dict.fromkeys(path.parent for path, _ in ordered):
            fsync_directory(directory)
    except BaseException as original_error:
        rollback_error: BaseException | None = None
        if replacement_started:
            for path, _ in reversed(ordered):
                try:
                    restore_original(path, captured[path])
                except BaseException as exc:
                    rollback_error = rollback_error or exc
        if rollback_error is not None:
            try:
                original_error.add_note(f"rollback also failed: {rollback_error}")
            except AttributeError:
                pass
        raise
    finally:
        for temporary in temporaries.values():
            temporary.unlink(missing_ok=True)


def atomic_replace_bytes(path: Path, data: bytes) -> None:
    atomic_replace_many({Path(path): data})
