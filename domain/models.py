"""FIDE domain data contracts. Pure Python — no Flask, SQLAlchemy, I/O."""

from dataclasses import dataclass
from typing import Optional

#: Canonical rating types, also the processing order for import jobs:
#: a type is eligible only when every earlier type has completed.
RATING_TYPES = ("standard", "rapid", "blitz")
RATING_TYPE_ORDER = RATING_TYPES

#: Job lifecycle states. ``verifying`` is a FIDE-Service addition on top of
#: the Swiss pipeline (post-import count/consistency check before cleanup).
JOB_STATES = (
    "pending",
    "downloading",
    "validating",
    "importing",
    "verifying",
    "completed",
    "failed",
)

#: States owned by a live worker step (heartbeat must stay fresh).
ACTIVE_JOB_STATES = ("downloading", "validating", "importing", "verifying")

#: Allowed state transitions. ``failed -> pending`` happens ONLY through an
#: explicit retry; ``completed`` is terminal and never reprocessed.
JOB_TRANSITIONS = {
    "pending": ("downloading",),
    "downloading": ("validating", "failed"),
    "validating": ("importing", "failed"),
    "importing": ("verifying", "failed"),
    "verifying": ("completed", "failed"),
    "completed": (),
    "failed": ("pending",),
}


def can_transition(from_state: str, to_state: str) -> bool:
    """True when a job may move from ``from_state`` to ``to_state``."""
    return to_state in JOB_TRANSITIONS.get(from_state, ())


@dataclass
class FidePlayerData:
    """One parsed FIDE player record (in-memory only, never persisted)."""

    fide_id: str
    name: str
    federation: str
    sex: str
    title: Optional[str] = None
    wtitle: Optional[str] = None
    otitle: Optional[str] = None
    foatitle: Optional[str] = None
    rating_standard: int = 0
    games_standard: int = 0
    k_standard: int = 20
    rating_rapid: int = 0
    games_rapid: int = 0
    k_rapid: int = 20
    rating_blitz: int = 0
    games_blitz: int = 0
    k_blitz: int = 20
    birth_year: Optional[str] = None
    inactive: bool = False

    def rating_for(self, rating_type: str):
        """(rating, games, k) triple for one rating type."""
        if rating_type == "standard":
            return (self.rating_standard, self.games_standard, self.k_standard)
        if rating_type == "rapid":
            return (self.rating_rapid, self.games_rapid, self.k_rapid)
        if rating_type == "blitz":
            return (self.rating_blitz, self.games_blitz, self.k_blitz)
        raise ValueError("unknown rating_type: %r" % (rating_type,))
