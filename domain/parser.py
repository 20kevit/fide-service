"""Streaming FIDE XML rating-list parser. Pure Python, no I/O beyond reads.

Memory stays bounded: ``iterparse`` + ``root.clear()`` per <player>, so
the ~847 MB official files never load into RAM. Accepts a file path or
an already-open binary stream (e.g. a ZIP member opened directly from
the monthly archive — the XML is never extracted to disk).

Official tag spellings (``w_title``/``o_title``/``foa_title``/``birthday``)
win; legacy aliases (``wtitle``/``birth_year``/…) remain accepted.
"""
import xml.etree.ElementTree as ET
from typing import BinaryIO, Generator, List, Optional, Union

from domain.models import FidePlayerData, RATING_TYPES


def _first_text(elem: ET.Element, *tags: str, default=None):
    """First non-empty text among candidate tag spellings."""
    for tag in tags:
        val = elem.findtext(tag)
        if val:
            return val
    return default


def _iter_player_elements(source: Union[str, BinaryIO]):
    """Yield (root, elem) for each finished <player>; caller clears root."""
    context = ET.iterparse(source, events=("start", "end"))
    _, root = next(context)
    for event, elem in context:
        if event == "end" and elem.tag == "player":
            yield root, elem


def parse_fide_xml(
    source: Union[str, BinaryIO],
    allowed_federations: Optional[List[str]] = None,
    rating_type: Optional[str] = None,
) -> Generator[FidePlayerData, None, None]:
    """Yield FidePlayerData records from a FIDE XML source.

    ``allowed_federations`` filters rows early (None/empty = all —
    the service default; history scoping happens at persist time, not
    here). ``rating_type`` isolates one category: explicit
    ``<rapid_rating>``-style tags win, otherwise the file's generic
    ``<rating>/<games>/<k>`` tags are interpreted as this category, so a
    Rapid file can never contribute a Standard rating.
    """
    if rating_type is not None and rating_type not in RATING_TYPES:
        raise ValueError("rating_type must be one of %s" % (RATING_TYPES,))
    fed_filter = set(allowed_federations) if allowed_federations else None

    for root, elem in _iter_player_elements(source):
        country = elem.findtext("country", "")
        if fed_filter and country not in fed_filter:
            root.clear()
            continue

        fide_id = (elem.findtext("fideid", "") or "").strip()
        if not fide_id:
            root.clear()
            continue

        name = elem.findtext("name", "")
        sex = elem.findtext("sex", "M")

        def _get_int(*tags: str) -> int:
            val = _first_text(elem, *tags, default="0")
            try:
                return int(val) if val else 0
            except ValueError:
                return 0

        def _has_any(*tags: str) -> bool:
            return any(elem.findtext(t) is not None for t in tags)

        if rating_type is None or rating_type == "standard":
            rs, gs, ks = (_get_int("rating"), _get_int("games"),
                          _get_int("k"))
        else:
            rs, gs, ks = 0, 0, 20
        if rating_type is None:
            rr, gr, kr = (_get_int("rapid_rating"), _get_int("rapid_games"),
                          _get_int("rapid_k"))
        elif rating_type == "rapid":
            if _has_any("rapid_rating", "rapid_games", "rapid_k"):
                rr, gr, kr = (_get_int("rapid_rating"),
                              _get_int("rapid_games"), _get_int("rapid_k"))
            else:
                rr, gr, kr = (_get_int("rating"), _get_int("games"),
                              _get_int("k"))
        else:
            rr, gr, kr = 0, 0, 20
        if rating_type is None:
            rb, gb, kb = (_get_int("blitz_rating"), _get_int("blitz_games"),
                          _get_int("blitz_k"))
        elif rating_type == "blitz":
            if _has_any("blitz_rating", "blitz_games", "blitz_k"):
                rb, gb, kb = (_get_int("blitz_rating"),
                              _get_int("blitz_games"), _get_int("blitz_k"))
            else:
                rb, gb, kb = (_get_int("rating"), _get_int("games"),
                              _get_int("k"))
        else:
            rb, gb, kb = 0, 0, 20

        yield FidePlayerData(
            fide_id=fide_id,
            name=name,
            federation=country,
            sex=sex,
            title=elem.findtext("title"),
            wtitle=_first_text(elem, "w_title", "wtitle"),
            otitle=_first_text(elem, "o_title", "otitle"),
            foatitle=_first_text(elem, "foa_title", "foatitle"),
            rating_standard=rs,
            games_standard=gs,
            k_standard=ks,
            rating_rapid=rr,
            games_rapid=gr,
            k_rapid=kr,
            rating_blitz=rb,
            games_blitz=gb,
            k_blitz=kb,
            birth_year=_first_text(elem, "birthday", "birth_year"),
            inactive=(elem.findtext("flag") == "inactive"),
        )
        root.clear()
