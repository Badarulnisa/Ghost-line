"""
discovery_loop.py
Orchestrates one investigation cycle. Looks at what seeds are currently
known, recommends a search strategy (direct lookup vs variant
generation vs both), and executes whichever the user confirms.

No print()/input() for the actual choice UI -- that stays in the
interaction layer -- but assess_strategy() returns a plain-data
recommendation object the CLI layer renders however it wants.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone

from variant_engine import SeedIdentity, generate_variants
from wmn_wrapper import check_many_usernames_detailed
from investigation import Investigation, Lead, CycleRecord


@dataclass
class StrategyRecommendation:
    """What the tool suggests doing this cycle, and why."""
    direct_targets: list[str] = field(default_factory=list)   # exact strings to check as-is
    variant_targets: list[str] = field(default_factory=list)  # generated guesses (optional)
    reasoning: str = ""
    default_choice: str = "direct"  # "direct" | "variants" | "both" | "none"


def assess_strategy(inv: Investigation, max_variants: int = 100) -> StrategyRecommendation:
    """
    Looks at current seeds and recommends what to search this cycle.
    Does not execute anything -- pure assessment.
    """
    has_handles = bool(inv.handles)
    has_name = bool(inv.names)

    direct = list(dict.fromkeys(inv.handles))  # dedupe, preserve order

    if has_handles and not has_name:
        return StrategyRecommendation(
            direct_targets=direct,
            variant_targets=[],
            reasoning=f"You have {len(direct)} confirmed handle(s) and no name on file. "
                      f"Recommend checking these directly -- no guessing needed.",
            default_choice="direct",
        )

    if has_handles and has_name:
        seed = SeedIdentity(names=inv.names, birth_year=inv.birth_year,
                             location=inv.locations[-1] if inv.locations else None)
        variants = generate_variants(seed, max_variants=max_variants)
        return StrategyRecommendation(
            direct_targets=direct,
            variant_targets=variants,
            reasoning=f"You have {len(direct)} confirmed handle(s) plus a name. "
                      f"Recommend checking the handle(s) directly first -- that's your "
                      f"highest-confidence data. Variant generation from the name "
                      f"({len(variants)} candidates) is available as an optional "
                      f"secondary pass if you want broader coverage, but it's likely "
                      f"to add noise at this point, not signal.",
            default_choice="direct",
        )

    if not has_handles and has_name:
        seed = SeedIdentity(names=inv.names, birth_year=inv.birth_year,
                             location=inv.locations[-1] if inv.locations else None,
                             extra_tokens=inv.locations[:-1])
        variants = generate_variants(seed, max_variants=max_variants)
        return StrategyRecommendation(
            direct_targets=[],
            variant_targets=variants,
            reasoning=f"You only have a name (no confirmed handle yet). Nothing concrete "
                      f"to search directly, so recommend variant generation "
                      f"({len(variants)} candidates) to find a starting handle.",
            default_choice="variants",
        )

    return StrategyRecommendation(
        reasoning="No usable seeds yet (no name, no handle, no email). Add at least one before running a cycle.",
        default_choice="none",
    )


async def run_cycle(inv: Investigation, dataset: dict, targets: list[str],
                     concurrency: int, timeout: float):
    """
    Executes a check against an explicit target list (already decided by
    the user via a StrategyRecommendation) -- no guessing happens here,
    this function just runs whatever list it's given.
    """
    inv.cycle += 1

    results = await check_many_usernames_detailed(
        targets, dataset, concurrency=concurrency, timeout=timeout
    )

    hit_count = sum(r.hit_count for r in results.values())
    inv.all_hits[str(inv.cycle)] = {
        u: [h.to_dict() for h in r.hits] for u, r in results.items() if r.hits
    }
    inv.history.append(CycleRecord(
        cycle=inv.cycle,
        timestamp=datetime.now(timezone.utc).isoformat(),
        seeds_snapshot={"names": inv.names, "handles": inv.handles,
                        "locations": inv.locations, "emails": inv.emails},
        candidates_checked=len(targets),
        hits_found=hit_count,
    ))
    return results


def accept_pivot(inv: Investigation, source: str, value: str, kind: str, note: str = "") -> None:
    """Fold a chosen hit (or a manually-typed new lead) back into the seed pool."""
    lead = Lead(source=source, value=value, kind=kind, added_cycle=inv.cycle, note=note)
    inv.leads.append(lead)
    inv.history[-1].pivots_selected.append(source)

    if kind in ("username", "handle") and value not in inv.handles:
        inv.handles.append(value)
    elif kind == "email" and value not in inv.emails:
        inv.emails.append(value)
    elif kind == "location" and value not in inv.locations:
        inv.locations.append(value)
    elif kind == "name" and value not in inv.names:
        inv.names.append(value)
