"""
confidence.py
Assigns a categorical confidence label to each hit, based on signals that
are cheap/available now (match type, site reliability metadata) plus a
slot for signals the correlation engine adds later (bio/avatar match).

Deliberately categorical, not numeric. A hit either has real reasons to
trust it or it doesn't -- a fake-precise score like "0.73" implies more
rigor than these signals actually support. The label is always paired
with a plain-English reason list so the tier is inspectable, not a black
box.

Labels, low to high: null, below_average, average, above_average, strong.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum


class ConfidenceLabel(str, Enum):
    NULL = "null"                    # no positive signal at all
    BELOW_AVERAGE = "below average"
    AVERAGE = "average"
    ABOVE_AVERAGE = "above average"
    STRONG = "strong"


# Ordered so labels can be compared/sorted (STRONG first when ranking hits)
_ORDER = [ConfidenceLabel.STRONG, ConfidenceLabel.ABOVE_AVERAGE,
          ConfidenceLabel.AVERAGE, ConfidenceLabel.BELOW_AVERAGE, ConfidenceLabel.NULL]


@dataclass
class ConfidenceAssessment:
    label: ConfidenceLabel
    reasons: list[str] = field(default_factory=list)  # why this label, in plain English

    def __str__(self) -> str:
        return self.label.value


def assess_hit_confidence(
    *,
    is_direct_match: bool,
    site_protection: list[str] | None,
    site_known_accounts_verified: bool = False,
    bio_matches_seed: bool | None = None,   # None = not checked yet (no fetch done)
    avatar_matches_confirmed: bool | None = None,  # None = not checked yet
) -> ConfidenceAssessment:
    """
    Computes a confidence label from whatever signals are actually
    available for this hit. Signals that require a profile-content fetch
    (bio_matches_seed, avatar_matches_confirmed) are optional -- pass None
    if that fetch hasn't happened, and this degrades gracefully to
    match-type + site-reliability signals only.
    """
    reasons: list[str] = []
    points = 0

    if is_direct_match:
        points += 2
        reasons.append("checked your exact known handle, not a guess")
    else:
        reasons.append("found via a generated username guess, not a confirmed handle")

    protected = bool(site_protection)
    if protected:
        points -= 1
        reasons.append(
            f"site uses anti-bot protection ({', '.join(site_protection)}) -- "
            f"existence check is less certain here"
        )
    else:
        points += 1
        reasons.append("site has no known anti-bot interference flagged")

    if bio_matches_seed is True:
        points += 3
        reasons.append("profile bio/text matches known name or location")
    elif bio_matches_seed is False:
        points -= 2
        reasons.append("profile bio/text does NOT match known details")
    # None -> no change, no claim made

    if avatar_matches_confirmed is True:
        points += 4
        reasons.append("profile photo matches another confirmed hit")
    elif avatar_matches_confirmed is False:
        points -= 1
        reasons.append("profile photo does not match other confirmed hits")

    if points <= 0:
        label = ConfidenceLabel.NULL
    elif points <= 1:
        label = ConfidenceLabel.BELOW_AVERAGE
    elif points <= 3:
        label = ConfidenceLabel.AVERAGE
    elif points <= 5:
        label = ConfidenceLabel.ABOVE_AVERAGE
    else:
        label = ConfidenceLabel.STRONG

    return ConfidenceAssessment(label=label, reasons=reasons)


def sort_by_confidence(items: list, label_getter) -> list:
    """Sorts a list of items (e.g. hits) by confidence label, strongest first.
    label_getter(item) -> ConfidenceLabel"""
    return sorted(items, key=lambda x: _ORDER.index(label_getter(x)))
