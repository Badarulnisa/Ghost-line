"""
discovery_loop.py
Orchestrates one investigation cycle: given an explicit list of target
usernames to check, runs them, records results, and updates investigation
state. Also provides assess_strategy(), which turns accumulated seed data
into two concrete target lists (direct handle checks vs. generated
variants) for the CLI layer to present as choices.

No print()/input() here -- pure orchestration, kept separate from the
interaction layer in cli_interactive.py.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone

from variant_engine import SeedIdentity, generate_variants
from wmn_wrapper import check_many_usernames_detailed
from investigation import Investigation, Lead, CycleRecord


@dataclass
class StrategyRecommendation:
    direct_targets: list[str]
    variant_targets: list[str]


def assess_strategy(inv: Investigation, max_variants: int = 100) -> StrategyRecommendation:
    direct_targets = list(dict.fromkeys(inv.handles))

    variant_targets: list[str] = []
    if inv.names:
        seed = SeedIdentity(
            names=inv.names,
            known_handle=inv.handles[-1] if inv.handles else None,
            birth_year=inv.birth_year,
            location=inv.locations[-1] if inv.locations else None,
            extra_tokens=inv.handles[:-1] + inv.locations[:-1],
        )
        variant_targets = generate_variants(seed, max_variants=max_variants)

    return StrategyRecommendation(direct_targets=direct_targets, variant_targets=variant_targets)


async def run_cycle(
    inv: Investigation,
    dataset: dict,
    targets: list[str],
    concurrency: int = 60,
    timeout: float = 10.0,
):
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
        seeds_snapshot={"names": inv.names, "handles": inv.handles, "locations": inv.locations},
        candidates_checked=len(targets),
        hits_found=hit_count,
    ))
    return results


def accept_pivot(inv: Investigation, source: str, value: str, kind: str, note: str = "") -> None:
    lead = Lead(source=source, value=value, kind=kind, added_cycle=inv.cycle, note=note)
    inv.leads.append(lead)
    if inv.history:
        inv.history[-1].pivots_selected.append(source)

    if kind in ("username", "handle") and value not in inv.handles:
        inv.handles.append(value)
    elif kind == "email" and value not in inv.emails:
        inv.emails.append(value)
    elif kind == "location" and value not in inv.locations:
        inv.locations.append(value)
    elif kind == "name" and value not in inv.names:
        inv.names.append(value)