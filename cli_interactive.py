"""
cli_interactive.py — Ghost-line, iterative pivot mode

Runs discovery cycles, lets you pick which hits to pivot on, and
persists state to investigation.json between runs. Kept separate
from cli.py (single-shot mode) so both can coexist.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from investigation import load_or_create, save
from discovery_loop import run_cycle, accept_pivot
from wmn_wrapper import fetch_dataset


async def main():
    inv = load_or_create("investigation.json", case_name="case_001")
    dataset = await fetch_dataset(refresh=True)

    while True:
        results = await run_cycle(inv, dataset, max_variants=200, concurrency=60, timeout=10.0)
        save(inv, "investigation.json")

        flat_hits = [(u, h) for u, r in results.items() for h in r.hits]
        if not flat_hits:
            print("No new hits this cycle.")
        for i, (u, h) in enumerate(flat_hits):
            print(f"[{i}] {u} -> {h.site_name} ({h.url})")

        choice = input("Pivot on index (or blank to stop): ").strip()
        if not choice:
            break
        u, h = flat_hits[int(choice)]
        accept_pivot(inv, source=f"hit:{u}:{h.site_name}", value=u, kind="handle")
        save(inv, "investigation.json")


if __name__ == "__main__":
    asyncio.run(main())