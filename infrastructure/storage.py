"""FIDE source-file storage. No framework imports (stdlib logging only).

The ~847 MB official XML is NEVER extracted: it streams member-by-member
straight from the ~50 MB archive via ZipFile.open() + iterparse. Only one
step runs at a time, so at most one tmp ZIP per (period, type) exists;
the worker deletes it after success and keeps it after failure for
diagnosis/retry. Period directories older than the retention window are
swept by cleanup_old_files().
"""
import logging
import os
import zipfile
from datetime import datetime, timedelta
from typing import BinaryIO, Callable, Optional, Tuple

from domain.errors import FideError, FideErrorCategory

log = logging.getLogger(__name__)

JOB_TMP_TYPES = ("standard", "rapid", "blitz")


class FideStorageError(Exception):
    """Invalid/unsafe archive contents."""


class _ZipMemberStream:
    """Fresh, self-closing read stream for one ZIP member.

    Each instance opens its own ZipFile handle (central-directory read
    is tiny) because a ZipExtFile cannot outlive its parent. Callers
    MUST close the stream. Only .read() is needed by iterparse.
    """

    def __init__(self, zip_path: str, member: str):
        self._zf = zipfile.ZipFile(zip_path, "r")
        self._stream = self._zf.open(member, "r")

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            self._zf.close()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def get_period_string(now: Optional[datetime] = None) -> str:
    """Current period string YYYY-MM (UTC)."""
    return (now or datetime.utcnow()).strftime("%Y-%m")


def tmp_zip_path(base_dir: str, period: str, rating_type: str) -> str:
    """Deterministic staged path: <base>/tmp/<YYYY-MM>/<type>.zip.

    Raises ValueError on bad input — never invent paths.
    """
    if rating_type not in JOB_TMP_TYPES:
        raise ValueError("rating_type must be one of %s" % (JOB_TMP_TYPES,))
    if not period or len(period) != 7 or period[4] != "-":
        raise ValueError("period must be YYYY-MM, got %r" % (period,))
    tmp_dir = os.path.join(base_dir, "tmp", period)
    os.makedirs(tmp_dir, exist_ok=True)
    return os.path.join(tmp_dir, "%s.zip" % rating_type)


def _safe_member_names(zip_ref: zipfile.ZipFile) -> list:
    """XML member names, rejecting path-traversal entries."""
    safe = []
    for name in zip_ref.namelist():
        base = os.path.basename(name)
        if not base.lower().endswith(".xml"):
            continue
        if base != name and (".." in name or name.startswith(("/", "\\"))):
            raise FideStorageError("Unsafe archive member: %s" % name)
        if ".." in name.split("/"):
            raise FideStorageError("Unsafe archive member: %s" % name)
        safe.append(name)
    return safe


def inspect_zip(zip_path: str) -> Tuple[str, int]:
    """Validate a ZIP WITHOUT extracting. Returns (member, size_bytes)."""
    if not os.path.isfile(zip_path):
        raise FideError(FideErrorCategory.FILE_NOT_FOUND, stage="extract",
                        detail="ZIP file not found: %s"
                        % os.path.basename(zip_path))
    with open(zip_path, "rb") as probe:
        if probe.read(4) != b"PK\x03\x04":
            raise FideStorageError("File is not a valid ZIP archive.")
    try:
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            members = _safe_member_names(zip_ref)
            if not members:
                raise FideStorageError(
                    "No XML file found inside the archive.")
            info = zip_ref.getinfo(members[0])
            return members[0], info.file_size
    except FideStorageError:
        raise
    except Exception as exc:
        log.error("ZIP inspection failed: %s", exc)
        raise FideStorageError("ZIP inspection failed: %s" % exc)


def open_zip_member(zip_path: str, member: str) -> _ZipMemberStream:
    """Open a FRESH read stream for one ZIP member. Caller MUST close."""
    return _ZipMemberStream(zip_path, member)


def count_players_in_stream(open_stream: Callable[[], BinaryIO]) -> int:
    """Count <player> end-tags through a fresh stream (bounded memory)."""
    import xml.etree.ElementTree as ET
    stream = open_stream()
    try:
        context = ET.iterparse(stream, events=("end",))
        count = 0
        for event, elem in context:
            if elem.tag == "player":
                count += 1
                elem.clear()
        return count
    finally:
        try:
            stream.close()
        except Exception:
            pass


def save_upload(file_storage, dest_path: str, max_bytes: int) -> int:
    """Persist an uploaded ZIP with size + magic validation. Returns size."""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    file_storage.save(dest_path)
    size = os.path.getsize(dest_path)
    if size == 0:
        os.remove(dest_path)
        raise FideError(FideErrorCategory.INVALID_XML, stage="upload",
                        detail="Uploaded file is empty.")
    if size > max_bytes:
        os.remove(dest_path)
        raise FideError(
            FideErrorCategory.FILE_TOO_LARGE, stage="upload",
            detail="Uploaded file is %d bytes; limit is %d bytes."
            % (size, max_bytes))
    with open(dest_path, "rb") as probe:
        if probe.read(4) != b"PK\x03\x04":
            os.remove(dest_path)
            raise FideError(
                FideErrorCategory.INVALID_CONTENT, stage="upload",
                detail="Uploaded file is not a ZIP archive.")
    return size


def cleanup_old_files(base_dir: str, retention_days: int = 90) -> int:
    """Delete tmp/<YYYY-MM> dirs older than the retention window.

    Returns the number of removed directories. Never raises for missing
    roots; logs and skips entries that fail.
    """
    tmp_root = os.path.join(base_dir, "tmp")
    if not os.path.isdir(tmp_root):
        return 0
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    removed = 0
    for entry in os.listdir(tmp_root):
        path = os.path.join(tmp_root, entry)
        if not os.path.isdir(path):
            continue
        try:
            stamp = datetime.strptime(entry, "%Y-%m")
        except ValueError:
            continue  # not a period dir — leave alone
        if stamp < cutoff.replace(day=1):
            import shutil
            try:
                shutil.rmtree(path)
                removed += 1
            except OSError as exc:
                log.warning("Retention cleanup failed for %s: %s", path, exc)
    return removed
