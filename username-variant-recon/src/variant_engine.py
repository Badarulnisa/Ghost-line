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
"""

from __future__ import annotations
from dataclasses import dataclass, field
from itertools import product
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


def _case_variants(s: str) -> list[str]:
    variants = {s.lower()}
    if s and s[0].isalpha():
        variants.add(s[0].upper() + s[1:].lower())
    return list(variants)


def generate_variants(seed: SeedIdentity, max_variants: int | None = 500) -> list[str]:
    """
    Returns a deduplicated, lowercase-normalized list of candidate usernames.

    max_variants caps output size (permutation space grows fast); pass
    None for unbounded (not recommended past 4-5 tokens).
    """
    names = [n.strip().lower() for n in seed.names if n.strip()]
    if not names:
        raise ValueError("SeedIdentity.names must contain at least one name token")

    tokens = list(names)

    numeric_suffixes = set(COMMON_SUFFIXES) | set(_year_suffixes(seed.birth_year))
    location_tokens = [seed.location.strip().lower()] if seed.location else []
    profession_tokens = [seed.profession.strip().lower()] if seed.profession else []
    extra = [t.strip().lower() for t in seed.extra_tokens if t.strip()]

    candidates: set[str] = set()

    # 1. Name-only concatenations across orderings and separators
    if len(tokens) >= 2:
        orderings = {tuple(tokens), tuple(reversed(tokens))}
    else:
        orderings = {tuple(tokens)}

    for order in orderings:
        for sep in SEPARATORS:
            base = sep.join(order)
            candidates.add(base)
            for suf in numeric_suffixes:
                if suf:
                    candidates.add(f"{base}{suf}")

    # 2. Single-token mononym style (common in South Asian / MENA handles:
    #    people often go by one name online rather than first.last)
    for tok in tokens:
        candidates.add(tok)
        for suf in numeric_suffixes:
            if suf:
                candidates.add(f"{tok}{suf}")

    # 3. Name + location
    for order in orderings:
        base = "".join(order)
        for loc in location_tokens:
            candidates.add(f"{base}{loc}")
            candidates.add(f"{loc}{base}")
            candidates.add(f"{base}.{loc}")
            candidates.add(f"{base}_{loc}")

    # 4. Name + profession
    for order in orderings:
        base = "".join(order)
        for prof in profession_tokens:
            candidates.add(f"{base}{prof}")
            candidates.add(f"{prof}{base}")
            candidates.add(f"{base}.{prof}")

    # 5. Known handle as a seed for further mutation (strip trailing digits,
    #    try alternate separators on the same root)
    if seed.known_handle:
        root = seed.known_handle.lower()
        candidates.add(root)
        stripped = root.rstrip("0123456789")
        if stripped and stripped != root:
            candidates.add(stripped)
            for suf in numeric_suffixes:
                if suf:
                    candidates.add(f"{stripped}{suf}")

    # 6. User-supplied extra tokens (nicknames/aliases), same treatment as names
    for tok in extra:
        candidates.add(tok)
        for suf in numeric_suffixes:
            if suf:
                candidates.add(f"{tok}{suf}")

    # Normalize: strip empties, dedupe casing
    cleaned = sorted({c for c in candidates if c})

    if max_variants is not None and len(cleaned) > max_variants:
        cleaned = cleaned[:max_variants]

    return cleaned


if __name__ == "__main__":
    # quick manual smoke test
    seed = SeedIdentity(
        names=["ahmed", "chaudhary"],
        known_handle="chaudharyahmed07",
        location="lahore",
    )
    for v in generate_variants(seed):
        print(v)
