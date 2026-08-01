"""Crash-safe file transactions and runtime recovery guards."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import threading
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping

try:  # Linux deployment and macOS development hosts both provide fcntl.
    import fcntl
except ImportError:  # pragma: no cover - unsupported deployment platform
    fcntl = None


JOURNAL_PREFIX = ".stock-portfolio-txn-"
JOURNAL_GLOB = JOURNAL_PREFIX + "*.json"
_JOURNAL_VERSION = 1
_PROCESS_LOCK = threading.RLock()


class AtomicTransactionError(OSError):
    """Raised when a durable transaction cannot be validated or recovered."""


@dataclass(frozen=True)
class OriginalFile:
    existed: bool
    data: bytes | None


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
            raise AtomicTransactionError(f"missing original bytes for {path}")
        temporary = write_fsync_temp(path, original.data)
        try:
            replace_path(temporary, path)
            fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return
    path.unlink(missing_ok=True)
    fsync_directory(path.parent)


def _open_new_durable(path: Path, data: bytes, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, mode)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_regular_file(path: Path, label: str) -> bytes:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise AtomicTransactionError(f"cannot inspect {label} {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AtomicTransactionError(f"{label} must be a regular non-symlink file: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise AtomicTransactionError(f"cannot read {label} {path}: {exc}") from exc


def _journal_paths(transaction_id: str, participants: Iterable[Path]) -> tuple[Path, ...]:
    directories = sorted({path.parent for path in participants}, key=str)
    return tuple(
        directory / f"{JOURNAL_PREFIX}{transaction_id}.json"
        for directory in directories
    )


def _encode_manifest(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _write_manifest_copies(paths: tuple[Path, ...], encoded: bytes) -> None:
    for path in paths:
        _open_new_durable(path, encoded)
    for directory in dict.fromkeys(path.parent for path in paths):
        fsync_directory(directory)


def _remove_paths(paths: Iterable[Path]) -> None:
    directories: list[Path] = []
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        directories.append(path.parent)
    for directory in dict.fromkeys(directories):
        fsync_directory(directory)


def _manifest_for(
    transaction_id: str,
    ordered: tuple[tuple[Path, bytes], ...],
    originals: Mapping[Path, OriginalFile],
    backups: Mapping[Path, Path | None],
    temporaries: Mapping[Path, Path],
) -> tuple[dict[str, object], tuple[Path, ...], Path]:
    journals = _journal_paths(transaction_id, (path for path, _ in ordered))
    commit_marker = journals[0].with_suffix(".commit")
    participants = []
    for path, target in ordered:
        original = originals[path]
        backup = backups[path]
        if original.existed and backup is None:
            raise AtomicTransactionError(f"durable transaction lacks backup for {path}")
        participants.append(
            {
                "path": str(path),
                "original_existed": original.existed,
                "original_sha256": (
                    None if original.data is None else _sha256(original.data)
                ),
                "backup_path": None if backup is None else str(_absolute(backup)),
                "temporary_path": str(_absolute(temporaries[path])),
                "target_sha256": _sha256(target),
            }
        )
    manifest: dict[str, object] = {
        "version": _JOURNAL_VERSION,
        "transaction_id": transaction_id,
        "participants": participants,
        "journal_paths": [str(path) for path in journals],
        "commit_marker": str(commit_marker),
    }
    return manifest, journals, commit_marker


def _parse_manifest(path: Path) -> dict[str, object]:
    raw = _read_regular_file(path, "transaction journal")

    def reject_constant(token: str) -> None:
        raise AtomicTransactionError(f"journal contains nonstandard number: {token}")

    try:
        payload = json.loads(raw, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AtomicTransactionError(f"invalid transaction journal {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != _JOURNAL_VERSION:
        raise AtomicTransactionError(f"unsupported transaction journal: {path}")
    transaction_id = payload.get("transaction_id")
    participants = payload.get("participants")
    journals = payload.get("journal_paths")
    marker = payload.get("commit_marker")
    if (
        type(transaction_id) is not str
        or not transaction_id
        or not isinstance(participants, list)
        or not participants
        or not isinstance(journals, list)
        or not journals
        or type(marker) is not str
        or not marker
    ):
        raise AtomicTransactionError(f"incomplete transaction journal: {path}")
    return payload


def _participant_paths(manifest: Mapping[str, object]) -> tuple[Path, ...]:
    result = []
    participants = manifest["participants"]
    if not isinstance(participants, list):
        raise AtomicTransactionError("transaction participants must be an array")
    for raw in participants:
        if not isinstance(raw, dict) or type(raw.get("path")) is not str:
            raise AtomicTransactionError("invalid transaction participant")
        result.append(_absolute(Path(raw["path"])))
    return tuple(result)


@contextmanager
def locked_paths(paths: Iterable[Path]) -> Iterator[None]:
    unique = sorted({_absolute(Path(path)) for path in paths}, key=str)
    with _PROCESS_LOCK, ExitStack() as stack:
        for path in unique:
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = path.with_suffix(path.suffix + ".lock")
            handle = stack.enter_context(lock_path.open("a+", encoding="utf-8"))
            if fcntl is None:  # pragma: no cover - guarded deployment invariant
                raise RuntimeError("durable transactions require fcntl process locking")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            stack.callback(fcntl.flock, handle.fileno(), fcntl.LOCK_UN)
        yield


def _pending_journals(path: Path) -> tuple[Path, ...]:
    try:
        return tuple(sorted(path.parent.glob(JOURNAL_GLOB), key=str))
    except OSError as exc:
        raise AtomicTransactionError(f"cannot scan transaction journals: {exc}") from exc


def _recover_manifest(journal: Path, manifest: Mapping[str, object]) -> None:
    participants = manifest["participants"]
    if not isinstance(participants, list):
        raise AtomicTransactionError("transaction participants must be an array")
    journal_paths = tuple(Path(item) for item in manifest["journal_paths"])  # type: ignore[arg-type]
    commit_marker = Path(str(manifest["commit_marker"]))
    committed = commit_marker.exists()
    temporary_paths: list[Path] = []
    if committed:
        _read_regular_file(commit_marker, "transaction commit marker")
    for raw in participants:
        if not isinstance(raw, dict):
            raise AtomicTransactionError("invalid transaction participant")
        path = _absolute(Path(str(raw["path"])))
        temporary_paths.append(Path(str(raw["temporary_path"])))
        if committed:
            try:
                actual_hash = _sha256(_read_regular_file(path, "committed target"))
            except AtomicTransactionError as exc:
                raise AtomicTransactionError(
                    f"committed transaction target is unavailable: {path}"
                ) from exc
            if actual_hash != raw.get("target_sha256"):
                raise AtomicTransactionError(
                    f"committed transaction target hash differs: {path}"
                )
            continue
        original_existed = raw.get("original_existed")
        if type(original_existed) is not bool:
            raise AtomicTransactionError("invalid original existence flag")
        if not original_existed:
            restore_original(path, OriginalFile(False, None))
            continue
        backup_value = raw.get("backup_path")
        if type(backup_value) is not str or not backup_value:
            raise AtomicTransactionError(f"missing recovery backup for {path}")
        backup_data = _read_regular_file(Path(backup_value), "recovery backup")
        if _sha256(backup_data) != raw.get("original_sha256"):
            raise AtomicTransactionError(f"recovery backup hash differs for {path}")
        restore_original(path, OriginalFile(True, backup_data))
    _remove_paths(temporary_paths)
    _remove_paths(journal_paths)
    _remove_paths((commit_marker,))


def recover_pending_transactions(path: Path) -> None:
    """Recover every durable transaction discoverable beside ``path``."""

    seen: set[str] = set()
    while True:
        journals = _pending_journals(_absolute(Path(path)))
        pending = next((item for item in journals if str(item) not in seen), None)
        if pending is None:
            return
        manifest = _parse_manifest(pending)
        transaction_id = str(manifest["transaction_id"])
        participant_paths = _participant_paths(manifest)
        with locked_paths(participant_paths):
            available = next(
                (
                    candidate
                    for candidate in (Path(item) for item in manifest["journal_paths"])  # type: ignore[arg-type]
                    if candidate.exists()
                ),
                None,
            )
            if available is not None:
                current = _parse_manifest(available)
                if current.get("transaction_id") != transaction_id:
                    raise AtomicTransactionError("transaction journal identity changed")
                _recover_manifest(available, current)
        seen.add(str(pending))


@contextmanager
def transaction_guard(paths: Iterable[Path]) -> Iterator[None]:
    """Recover-before-read and close the recovery/lock race for runtime callers."""

    targets = tuple(_absolute(Path(path)) for path in paths)
    while True:
        for path in targets:
            recover_pending_transactions(path)
        retry = False
        with locked_paths(targets):
            if any(_pending_journals(path) for path in targets):
                retry = True
            else:
                yield
                return
        if not retry:  # pragma: no cover - loop exits through the yielded branch
            return


def atomic_replace_many(
    replacements: Mapping[Path, bytes],
    *,
    originals: Mapping[Path, OriginalFile] | None = None,
    recovery_backups: Mapping[Path, Path | None] | None = None,
    durable: bool = False,
) -> None:
    """Replace targets, optionally with a crash-recoverable multi-file journal."""

    if not replacements:
        return
    ordered = tuple((_absolute(Path(path)), data) for path, data in replacements.items())
    normalized_originals = (
        None
        if originals is None
        else {_absolute(Path(path)): value for path, value in originals.items()}
    )
    normalized_backups = (
        None
        if recovery_backups is None
        else {
            _absolute(Path(path)): (
                None if backup is None else _absolute(Path(backup))
            )
            for path, backup in recovery_backups.items()
        }
    )
    captured = (
        {path: capture_original(path) for path, _ in ordered}
        if normalized_originals is None
        else {path: normalized_originals[path] for path, _ in ordered}
    )
    backups = (
        {path: None for path, _ in ordered}
        if normalized_backups is None
        else {path: normalized_backups[path] for path, _ in ordered}
    )
    if durable and recovery_backups is None:
        raise AtomicTransactionError("durable transaction requires recovery backups")
    temporaries: dict[Path, Path] = {}
    journals: tuple[Path, ...] = ()
    commit_marker: Path | None = None
    replacement_started = False
    committed = False
    try:
        for path, data in ordered:
            temporaries[path] = write_fsync_temp(path, data)
        if durable:
            transaction_id = uuid.uuid4().hex
            manifest, journals, commit_marker = _manifest_for(
                transaction_id,
                ordered,
                captured,
                backups,
                temporaries,
            )
            _write_manifest_copies(journals, _encode_manifest(manifest))
        for path, _ in ordered:
            replacement_started = True
            replace_path(temporaries[path], path)
        for directory in dict.fromkeys(path.parent for path, _ in ordered):
            fsync_directory(directory)
        if durable:
            if commit_marker is None:
                raise AtomicTransactionError("durable transaction lacks commit marker")
            _open_new_durable(commit_marker, b"committed\n")
            fsync_directory(commit_marker.parent)
            committed = True
            _remove_paths(journals)
            _remove_paths((commit_marker,))
    except BaseException as original_error:
        marker_exists = commit_marker is not None and commit_marker.exists()
        rollback_error: BaseException | None = None
        if replacement_started and not committed and not marker_exists:
            for path, _ in reversed(ordered):
                try:
                    restore_original(path, captured[path])
                except BaseException as exc:
                    rollback_error = rollback_error or exc
        if journals and not marker_exists:
            try:
                _remove_paths(journals)
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
