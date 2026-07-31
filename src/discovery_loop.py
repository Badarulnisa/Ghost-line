"""
discovery_loop.py
Orchestrates one investigation cycle: generate variants from current
seeds, check them, record results. No print()/input() here — pure
orchestration, kept separate from the interaction layer.
"""
from __future__ import annotations
from datetime import datetime, timezone

from variant_engine import SeedIdentity, generate_variants
from wmn_wrapper import check_many_usernames_detailed
from investigation import Investigation, Lead, CycleRecord


async def run_cycle(inv: Investigation, dataset: dict, max_variants: int, concurrency: int, timeout: float):
    inv.cycle += 1

    seed = SeedIdentity(
        names=inv.names or ["_"],
        known_handle=inv.handles[-1] if inv.handles else None,
        birth_year=inv.birth_year,
        location=inv.locations[-1] if inv.locations else None,
        extra_tokens=inv.handles[:-1] + inv.locations[:-1],
    )
    candidates = generate_variants(seed, max_variants=max_variants)

    results = await check_many_usernames_detailed(
        candidates, dataset, concurrency=concurrency, timeout=timeout
    )

    hit_count = sum(r.hit_count for r in results.values())
    inv.all_hits[str(inv.cycle)] = {
        u: [h.to_dict() for h in r.hits] for u, r in results.items() if r.hits
    }
    inv.history.append(CycleRecord(
        cycle=inv.cycle,
        timestamp=datetime.now(timezone.utc).isoformat(),
        seeds_snapshot={"names": inv.names, "handles": inv.handles, "locations": inv.locations},
        candidates_checked=len(candidates),
        hits_found=hit_count,
    ))
    return results


def accept_pivot(inv: Investigation, source: str, value: str, kind: str, note: str = "") -> None:
    """Fold a chosen hit back into the seed pool for the next cycle."""
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