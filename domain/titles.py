"""Canonical FIDE title selection. Pure Python.

The service preserves all four official columns verbatim
(title/wtitle/otitle/foatitle). When API consumers need ONE display
title, this module is the SINGLE place that derives it — the priority
``title -> wtitle -> otitle -> foatitle`` (first non-empty wins) mirrors
the long-standing Swiss verification rule. Never duplicate this rule.
"""


def canonical_title(title=None, wtitle=None, otitle=None, foatitle=None) -> str:
    """Return the first non-empty official title, or "" when none."""
    for candidate in (title, wtitle, otitle, foatitle):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return ""
