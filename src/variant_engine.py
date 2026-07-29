"""
variant_engine.py
Generates candidate username permutations from seed identity data.

Design notes:
- Pure function generator, no I/O, no network calls — easy to unit test.
- Culturally-aware: supports name orderings beyond western first.last
  (e.g. patronymic-style, single mononym handles common in South Asian
  and Middle Eastern contexts) since western-name-pattern tools tend to
  under-generate for these naming conventions.
- Dedupe + normalize output so downstream WMN calls aren't wasted on
  duplicate strings.

v2 change: candidates are now generated into priority tiers instead of
one flat set. Previously, all candidates were pooled together and
max_variants truncated a plain sorted() list — since sorted() orders
alphabetically, that meant the cutoff was arbitrary with respect to
which candidates were actually likely to hit. A known_handle-derived
candidate (seeded from a *confirmed real handle*) could get sliced off
in favor of a raw name+suffix permutation purely because it sorted
later. Tiers fix this: known_handle mutations > contextual (name+
location/profession) > name-only concatenations > mononym > extra/
nickname tokens. Truncation now drops the lowest-priority tier(s)
first, so a capped run keeps its best leads.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Iterable


SEPARATORS = ["", ".", "_", "-"]

# Common numeric suffixes people append beyond birth year:
# phone-like fragments, lucky numbers, sequential digits.
COMMON_SUFFIXES = ["", "1", "01", "007", "123", "99", "00"]


@dataclass
class SeedIdentity:
    """Raw known facts about the target. Only `names` is required."""
    names: list[str]                     # e.g. ["ahmed", "chaudhary"]
    known_handle: str | None = None      # confirmed real handle, e.g. "chaudharyahmed07"
    birth_year: str | None = None        # "1999" -> also tries "99"
    location: str | None = None          # "lahore"
    profession: str | None = None        # "developer", "photographer"
    extra_tokens: list[str] = field(default_factory=list)  # nicknames, aliases, etc.


def _year_suffixes(birth_year: str | None) -> list[str]:
    if not birth_year:
        return []
    out = [birth_year]
    if len(birth_year) == 4:
        out.append(birth_year[2:])  # 1999 -> 99
    return out


def _case_variants(s: str) -> set[str]:
    """
    Returns lowercase + capitalized forms of a candidate (e.g.
    "ahmedchaudhary" -> {"ahmedchaudhary", "Ahmedchaudhary"}).

    Most sites treat usernames as case-insensitive, but a meaningful
    minority don't (or display a case-sensitive vanity handle even if
    lookup is insensitive), so it's worth the small extra candidate
    count rather than silently skipping capitalized handles entirely.
    """
    if not s:
        return {s}
    variants = {s.lower()}
    if s[0].isalpha():
        variants.add(s[0].upper() + s[1:].lower())
    return variants


def _add_with_case(target: set[str], base: str) -> None:
    target.update(_case_variants(base))


def generate_variants(seed: SeedIdentity, max_variants: int | None = 500) -> list[str]:
    """
    Returns a deduplicated list of candidate usernames, highest-priority
    first (tier order), each tier internally sorted for determinism.

    max_variants caps output size (permutation space grows fast); pass
    None for unbounded (not recommended past 4-5 tokens). When capped,
    lower-priority tiers are dropped/truncated first so the highest-
    confidence candidates (those derived from a confirmed known_handle,
    then name+context, then plain name permutations, then mononym,
    then extra tokens) are never the ones lost to the cap.
    """
    names = [n.strip().lower() for n in seed.names if n.strip()]
    if not names:
        raise ValueError("SeedIdentity.names must contain at least one name token")

    tokens = list(names)

    numeric_suffixes = set(COMMON_SUFFIXES) | set(_year_suffixes(seed.birth_year))
    location_tokens = [seed.location.strip().lower()] if seed.location else []
    profession_tokens = [seed.profession.strip().lower()] if seed.profession else []
    extra = [t.strip().lower() for t in seed.extra_tokens if t.strip()]

    if len(tokens) >= 2:
        orderings = {tuple(tokens), tuple(reversed(tokens))}
    else:
        orderings = {tuple(tokens)}

    # Tiers, highest priority first. Each is a set to dedupe within itself;
    # cross-tier dedup happens at assembly time so a candidate only ever
    # counts toward the first (highest) tier it appears in.
    tier_known_handle: set[str] = set()
    tier_contextual: set[str] = set()      # name + location / name + profession
    tier_name_concat: set[str] = set()     # name-only concatenations, all separators
    tier_mononym: set[str] = set()         # single-token handles
    tier_extra: set[str] = set()           # user-supplied nicknames/aliases

    # Tier 0: known handle as a seed for further mutation (strip trailing
    # digits, try alternate separators on the same root). Highest priority
    # because it's derived from a *confirmed real handle*, not a guess.
    if seed.known_handle:
        root = seed.known_handle.strip().lower()
        _add_with_case(tier_known_handle, root)
        stripped = root.rstrip("0123456789")
        if stripped and stripped != root:
            _add_with_case(tier_known_handle, stripped)
            for suf in numeric_suffixes:
                if suf:
                    _add_with_case(tier_known_handle, f"{stripped}{suf}")

    # Tier 1: name + location, name + profession — more specific than a
    # bare name permutation, so more likely to be a deliberate choice.
    for order in orderings:
        base = "".join(order)
        for loc in location_tokens:
            _add_with_case(tier_contextual, f"{base}{loc}")
            _add_with_case(tier_contextual, f"{loc}{base}")
            _add_with_case(tier_contextual, f"{base}.{loc}")
            _add_with_case(tier_contextual, f"{base}_{loc}")
        for prof in profession_tokens:
            _add_with_case(tier_contextual, f"{base}{prof}")
            _add_with_case(tier_contextual, f"{prof}{base}")
            _add_with_case(tier_contextual, f"{base}.{prof}")

    # Tier 2: name-only concatenations across orderings and separators.
    for order in orderings:
        for sep in SEPARATORS:
            base = sep.join(order)
            _add_with_case(tier_name_concat, base)
            for suf in numeric_suffixes:
                if suf:
                    _add_with_case(tier_name_concat, f"{base}{suf}")

    # Tier 3: single-token mononym style (common in South Asian / MENA
    # handles: people often go by one name online rather than first.last).
    for tok in tokens:
        _add_with_case(tier_mononym, tok)
        for suf in numeric_suffixes:
            if suf:
                _add_with_case(tier_mononym, f"{tok}{suf}")

    # Tier 4: user-supplied extra tokens (nicknames/aliases), same
    # treatment as names. Lowest priority since these are the least
    # constrained by known facts about the target.
    for tok in extra:
        _add_with_case(tier_extra, tok)
        for suf in numeric_suffixes:
            if suf:
                _add_with_case(tier_extra, f"{tok}{suf}")

    tiers: list[set[str]] = [
        tier_known_handle,
        tier_contextual,
        tier_name_concat,
        tier_mononym,
        tier_extra,
    ]

    ordered: list[str] = []
    seen: set[str] = set()
    for tier in tiers:
        for c in sorted(tier):
            if c and c not in seen:
                seen.add(c)
                ordered.append(c)

    if max_variants is not None and len(ordered) > max_variants:
        ordered = ordered[:max_variants]

    return ordered


if __name__ == "__main__":
    # quick manual smoke test
    seed = SeedIdentity(
        names=["ahmed", "chaudhary"],
        known_handle="chaudharyahmed07",
        location="lahore",
    )
    for v in generate_variants(seed):
        print(v)