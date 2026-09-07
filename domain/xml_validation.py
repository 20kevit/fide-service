"""Pre-import XML validation. Pure Python — no framework imports.

Runs BEFORE any database write: well-formedness + FIDE structure +
security pre-scan (XXE/DOCTYPE rejection, HTML-disguised-as-XML
rejection), streaming so large files stay cheap. Raises classified
FideError so operators get WHAT/WHERE/WHY, never a bare traceback.
"""
import os
from dataclasses import dataclass
from typing import BinaryIO, Callable, Optional
import xml.etree.ElementTree as ET

from domain.errors import FideError, FideErrorCategory

KNOWN_ROOT_TAGS = {"playerslist", "players_list", "players"}
REQUIRED_PLAYER_FIELDS = ("fideid", "name", "country")

#: Refuse XML entity expansion (XXE / billion-laughs) outright.
_FORBIDDEN_MARKERS = ("<!ENTITY", "<!DOCTYPE")


@dataclass
class FideValidationResult:
    path: str
    size_bytes: int
    root_tag: str
    players_found: int
    sample_ok: bool


def _check_size(size: int, display_name: str,
                max_bytes: Optional[int]) -> None:
    if size == 0:
        raise FideError(FideErrorCategory.INVALID_XML, stage="validate",
                        detail="File is empty (0 bytes): %s" % display_name)
    if max_bytes and size > max_bytes:
        raise FideError(
            FideErrorCategory.FILE_TOO_LARGE, stage="validate",
            detail="File is %d bytes; limit is %d bytes." % (size, max_bytes))


def _check_head(head: bytes) -> None:
    """Binary pre-scan: reject entity attacks and non-XML uploads."""
    upper_head = head.decode("utf-8", errors="ignore").upper()
    for marker in _FORBIDDEN_MARKERS:
        if marker in upper_head:
            raise FideError(
                FideErrorCategory.INVALID_XML, stage="validate",
                detail="File contains a forbidden ENTITY/DOCTYPE declaration "
                       "and was rejected on security grounds.")
    stripped = head.lstrip()
    if not stripped.startswith(b"<"):
        raise FideError(
            FideErrorCategory.INVALID_XML, stage="validate",
            detail="Not valid XML: content does not start with an XML tag.")
    if b"<html" in head[:4096].lower():
        raise FideError(
            FideErrorCategory.INVALID_XML, stage="validate",
            detail="Not valid FIDE XML: content is an HTML page, not XML.")


def _check_structure(source) -> tuple:
    """Well-formedness + structure via streaming iterparse.

    Returns (root_tag, players_found, sample_ok). Stream callers MUST
    close their own stream.
    """
    try:
        context = ET.iterparse(source, events=("start", "end"))
        _, root = next(context)
    except ET.ParseError as exc:
        raise FideError(FideErrorCategory.INVALID_XML, stage="validate",
                        detail="Malformed XML: %s" % exc)
    except Exception as exc:
        raise FideError(FideErrorCategory.FILE_READ_ERROR,
                        stage="validate", detail=str(exc))

    root_tag = root.tag
    if root_tag not in KNOWN_ROOT_TAGS:
        raise FideError(
            FideErrorCategory.INVALID_FIDE_FORMAT, stage="validate",
            detail="Expected root <playerslist>; saw <%s>." % root_tag)

    players_found = 0
    sample_ok = False
    try:
        for event, elem in context:
            if event == "end" and elem.tag == "player":
                players_found += 1
                if not sample_ok:
                    fideid = (elem.findtext("fideid") or "").strip()
                    name = (elem.findtext("name") or "").strip()
                    country = (elem.findtext("country") or "").strip()
                    if players_found <= 5 and (not fideid or not name or not country):
                        raise FideError(
                            FideErrorCategory.INVALID_FIDE_FORMAT,
                            stage="validate",
                            detail="Player records lack required fields "
                                   "(fideid/name/country).")
                    if fideid and name and country:
                        sample_ok = True
                root.clear()
                if players_found >= 2000 and sample_ok:
                    break
    except FideError:
        raise
    except ET.ParseError as exc:
        raise FideError(FideErrorCategory.INVALID_XML,
                        stage="validate", detail="Malformed XML: %s" % exc)
    except Exception as exc:
        raise FideError(FideErrorCategory.PARSER_ERROR,
                        stage="validate", detail=str(exc))

    if players_found == 0:
        raise FideError(
            FideErrorCategory.INVALID_FIDE_FORMAT, stage="validate",
            detail="No <player> records found in file.")
    if not sample_ok:
        raise FideError(
            FideErrorCategory.INVALID_FIDE_FORMAT, stage="validate",
            detail="Sample records lack required fields (fideid/name/country).")
    return root_tag, players_found, sample_ok


def _read_head(open_stream: Callable[[], BinaryIO]) -> bytes:
    try:
        stream = open_stream()
    except OSError as exc:
        raise FideError(FideErrorCategory.FILE_READ_ERROR,
                        stage="validate", detail=str(exc))
    try:
        return stream.read(64 * 1024)
    except OSError as exc:
        raise FideError(FideErrorCategory.FILE_READ_ERROR,
                        stage="validate", detail=str(exc))
    finally:
        try:
            stream.close()
        except Exception:
            pass


def validate_xml_path(path: str,
                      max_bytes: Optional[int] = None) -> FideValidationResult:
    """Validate an on-disk FIDE XML file. Returns a summary on success."""
    if not path or not os.path.isfile(path):
        raise FideError(
            FideErrorCategory.FILE_NOT_FOUND, stage="validate",
            detail="File not found: %s" % (os.path.basename(path or "?")))
    if not os.access(path, os.R_OK):
        raise FideError(
            FideErrorCategory.FILE_READ_ERROR, stage="validate",
            detail="File is not readable: %s" % os.path.basename(path))
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise FideError(FideErrorCategory.FILE_READ_ERROR,
                        stage="validate", detail=str(exc))
    _check_size(size, os.path.basename(path), max_bytes)
    _check_head(_read_head(lambda: open(path, "rb")))
    stream = open(path, "rb")
    try:
        root_tag, players_found, sample_ok = _check_structure(stream)
    finally:
        stream.close()
    return FideValidationResult(path=path, size_bytes=size, root_tag=root_tag,
                                players_found=players_found, sample_ok=sample_ok)


def validate_stream(stream_factory: Callable[[], BinaryIO],
                    display_name: str,
                    size_bytes: int = 0,
                    max_bytes: Optional[int] = None) -> FideValidationResult:
    """Validate XML read through a stream factory (e.g. a ZIP member).

    Each     ``stream_factory()`` call must return a FRESH binary stream at
    the start (two sequential passes: head pre-scan, then structure
    scan). Every stream is closed by the validator.
    """
    if size_bytes:
        _check_size(size_bytes, display_name, max_bytes)
    _check_head(_read_head(stream_factory))
    stream = stream_factory()
    try:
        root_tag, players_found, sample_ok = _check_structure(stream)
    finally:
        try:
            stream.close()
        except Exception:
            pass
    return FideValidationResult(path=display_name, size_bytes=size_bytes,
                                root_tag=root_tag,
                                players_found=players_found, sample_ok=sample_ok)
